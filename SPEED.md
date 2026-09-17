# Speeding Up Zigbee OTA Firmware Updates

## Executive Summary

The current design is dominated by application-layer stop-and-wait behavior: a 208 KB image divided into 48-byte payloads requires approximately 4,437 payload blocks, before retransmissions. Increasing payload size helps, but it cannot by itself explain or eliminate a measured rate of 0.5–2 blocks/s. The highest-value investigation is therefore to measure the request-to-response cycle and determine whether the delay is caused by the coordinator, the device’s next-request scheduling, sleepy-device polling, flash writes, retries, or a deliberate server throttle.

Zigbee2MQTT documents two directly relevant controls: `default_maximum_data_size`, defaulting to 50 bytes and constrained to 10–100 bytes, and `image_block_response_delay`, defaulting to 250 ms with a 50 ms minimum. Larger chunks improve speed but reduce network stability, and some devices reject values above 50 or 64 bytes.[^1][^2]

The proposed 48-to-64-byte firmware change is reasonable as an opt-in experiment on a recoverable bench fixture, but it should not be treated as the primary optimization or bundled into a production update without evidence. A 64-byte request only produces a 33.3% payload-count reduction relative to 48 bytes, and the actual gain will be smaller if the server, stack, security mode, or route limits the response to 40–50 bytes.

## What the 48-Byte Limit Means

Zigbee OTA uses a sequence of `Image Block Request` and `Image Block Response` messages. Because a Zigbee packet has a maximum physical payload and the OTA response contains MAC, network, APS, ZCL, OTA, and security overhead, only a limited number of firmware bytes fit in one unfragmented response. Silicon Labs describes the OTA transfer as block-by-block because the image is larger than one Zigbee packet.[^3][^4]

The exact safe payload is topology- and security-dependent. ST documents a maximum of 49 bytes with network security only and 40 bytes when both network and APS security are applied. ST also notes that conservative values are used because source-routing and other network overhead can further consume space.[^5]

This means that a firmware-side constant such as `OTA_IMAGE_MAX_DATA_SIZE = 48` may be a client-request ceiling rather than the final wire payload. The effective value is the minimum of the client request, server policy, stack maximum, and frame-size limit. A request for 64 bytes can therefore be silently reduced, fragmented, rejected, or trigger retries depending on the implementation.

## Direct Zigbee2MQTT Tuning

Zigbee2MQTT’s documented configuration is:

```yaml
ota:
  image_block_response_delay: 250
  default_maximum_data_size: 50
```

`image_block_response_delay` is the minimum delay between chunks. Zigbee2MQTT documents 250 ms as the default and 50 ms as the minimum, while warning that reducing the delay can require a more stable network and can cause issues or crashes.[^2]

`default_maximum_data_size` controls the requested image-chunk size. Zigbee2MQTT documents a default of 50 bytes, a supported range of 10–100 bytes, and device-specific behavior in which values above 50 or 64 bytes are refused or ignored.[^1][^2]

For a dedicated test network, a sensible progression is:

| Test | `default_maximum_data_size` | `image_block_response_delay` | Purpose |
|---|---:|---:|---|
| Baseline | 50 | 250 ms | Establish current behavior |
| Low-risk throttle test | 50 | 100 ms | Isolate delay as a bottleneck |
| Payload test | 64 | 100 ms | Measure whether the device accepts 64-byte requests |
| Aggressive test | 64 or 80 | 50 ms | Find the stability/throughput boundary |

The settings should be tested one variable at a time, with the same image, route, coordinator firmware, channel, and device placement. Because Zigbee2MQTT recommends updating one device at a time and during low network demand, the production network should not be used for aggressive tuning.[^2]

## Why the Observed Rate Is Suspicious

At 48 bytes per block, a 208 KB image requires approximately 4,437 blocks, not 4,344 if 208 KB means 208 × 1024 bytes. If “208 KB” is decimal 208,000 bytes, the count is approximately 4,334 blocks. The exact count should be taken from the OTA image’s `Total Image Size` field, including the OTA wrapper, rather than from a rounded UI value.

At 0.5–2 blocks/s, the transfer takes roughly 37 minutes to 2.5 hours for 4,344 blocks. That is far slower than the raw 250-kbit/s IEEE 802.15.4 physical rate and indicates that airtime is not the only constraint. Silicon Labs explicitly warns that fast transfers consume more bandwidth and can increase Zigbee retries, but that warning does not explain a sustained multi-second request cadence on a stable, always-on router.[^4]

Capture at least these timestamps for every block:

- `Image Block Request` transmitted by the fixture.
- Request received by the coordinator.
- `Image Block Response` transmitted by the coordinator.
- Response received by the fixture.
- Next `Image Block Request` transmitted by the fixture.
- Any MAC ACK, APS ACK, retry, timeout, route discovery, or `WAIT_FOR_DATA` response.
- Flash-write start and completion, if firmware instrumentation is available.

The key diagnostic split is:

- Large request-to-response time: coordinator, host software, server scheduling, or an intentional `image_block_response_delay`.
- Large response-to-next-request time: device-side delay, flash write, scheduler, radio turnaround, or sleepy polling.
- Repeated frames and missing acknowledgments: link quality, interference, route depth, coordinator congestion, or an MTU/fragmentation mismatch.
- Responses consistently smaller than requested: server or stack cap, security overhead, or device-specific compatibility policy.

## Firmware Changes Worth Testing

### 1. Make the requested size runtime-configurable

Replace the hard-coded ceiling with a runtime or build-time parameter that can be selected per device or per OTA session. Log both the requested maximum and the actual response length. A useful diagnostic record is:

```c
struct ota_block_metrics {
    uint32_t offset;
    uint8_t requested_size;
    uint8_t received_size;
    uint32_t request_to_response_ms;
    uint32_t response_to_next_request_ms;
    uint8_t retry_count;
};
```

Do not assume that a 64-byte request means a 64-byte wire response. The response length is the value that determines payload-count reduction.

### 2. Test 50, 56, 60, and 64 bytes

A binary jump from 48 to 64 can obscure the real limit. Test intermediate values because a stack may fit 50 or 56 bytes without fragmentation but fail at 64. ST’s documentation shows why the safe ceiling changes with security and network overhead.[^5]

The client should also clamp the requested size to the stack’s calculated maximum, preserve flash-write alignment requirements, and correctly handle a final short block. If the flash driver requires 4-, 8-, or 16-byte alignment, buffer and pad internally rather than increasing OTA packet size solely to satisfy flash programming granularity.

### 3. Reduce unnecessary response delay

If the client is an always-powered ceiling fixture and the network is otherwise quiet, 250 ms is likely worth testing below the default. Zigbee2MQTT supports a minimum documented value of 50 ms, and the setting exists specifically to limit request rate and network congestion.[^1][^2]

A safer production policy is adaptive throttling rather than a permanently aggressive value:

- Start at 100–150 ms.
- Increase delay after a timeout, APS failure, duplicate block, or retry burst.
- Decrease delay only after a sustained window of successful blocks.
- Abort or pause if the duplicate/retry rate exceeds a threshold.

This is more complicated than a fixed value, but it directly addresses the trade-off documented by Silicon Labs: faster transfer uses more bandwidth and may increase retries.[^4]

### 4. Optimize device-side scheduling and flash writes

If the gap is after the response arrives, increasing block size will not fix the root cause. Profile flash erase/write behavior, filesystem or bootloader buffering, event-loop scheduling, logging, and radio state transitions. Buffering several blocks before programming can help if each flash operation introduces a long synchronous stall, provided power-fail recovery and integrity checks remain correct.

Do not erase a large flash region synchronously between blocks. Pre-erase where safe, use asynchronous page programming, and ensure the OTA task yields without delaying the next request unnecessarily. Keep the OTA response path independent from LED effects, sensor polling, verbose logs, and other nonessential work.

### 5. Handle sleepy-device behavior explicitly

If the fixture is a sleepy end device rather than a router, parent buffering and poll timing can dominate the transfer. Silicon Labs explains that sleepy devices retrieve data by polling their parent, and that short-poll mode is used when the device must remain responsive. The short-poll interval generally needs to be below one second and below the parent’s indirect-transmission timeout, documented as 7.68 seconds in the relevant guidance.[^6]

For the OTA session, force fast/short polling before the first block request and retain it until the final block and `Upgrade End Response` are complete. Restore the normal long-poll interval afterward, including on timeout and abort paths. If the ceiling fixture is a router, verify that it is actually operating as a router and not spending time in an unexpected low-power mode.

## Protocol-Level Options

### APS fragmentation

Sending more than the unfragmented OTA payload limit may invoke APS fragmentation. This is not automatically faster: each larger response becomes several fragments, each with its own acknowledgment and reassembly state. NXP documentation describes fragmentation windows in which multiple fragments are sent before a fragmentation acknowledgment; a lost fragment can cause retransmission of the window.[^7]

Fragmentation is therefore a controlled experiment, not a default recommendation. It requires every relevant hop and endpoint to support fragmentation, sufficient reassembly buffers, compatible window/inter-frame settings, and correct handling of retries. A 64-byte request that causes two fragments may perform worse than a 48- or 50-byte unfragmented transfer.

### OTA page requests

Some Zigbee stacks support page-request modes that transfer a page as a series of responses with less application-level stop-and-wait overhead. NXP’s OTA documentation describes page size and response spacing parameters, with example defaults of 512 bytes and 300 ms.[^8]

Page requests are attractive if the firmware SDK and Zigbee2MQTT/coordinator path support them end to end. They are not a drop-in replacement for ordinary image-block requests: confirm that the client, coordinator, server implementation, intermediary routers, and test capture all support the same OTA page-request behavior. If the current stack lacks this feature, implementing it may be a larger and riskier change than tuning block size and delay.

### Delta OTA

A delta update can provide a much larger improvement than increasing block size when successive firmware versions are similar. Silicon Labs describes delta DFU as transmitting only differences between the current and new image, reducing transfer size, network load, and energy use.[^9]

Delta OTA requires a robust patch format, base-image identity checking, enough temporary storage, recovery behavior for power loss, and a fallback full-image update. It is best considered a product-level feature rather than a quick optimization to the current OTA path.

## Recommended Experiment Plan

1. **Freeze a known-good baseline.** Use the current b34 image, a fixed 208 KB OTA file, one coordinator, one fixture, a fixed channel, and a quiet network.
2. **Capture the traffic.** Use a 802.15.4/Zigbee sniffer if available; otherwise add timestamped firmware and coordinator logs.
3. **Measure actual sizes.** Record requested size, response size, request/response latency, response/next-request latency, retries, and final image size.
4. **Test server delay first.** Keep firmware at 48 bytes and test Zigbee2MQTT at 250, 150, 100, and 50 ms.
5. **Test firmware size second.** Hold delay constant and test 48, 50, 56, 60, and 64 bytes.
6. **Test combined settings.** Use the fastest individually stable size and delay together.
7. **Test fragmentation only on the bench.** Confirm whether 64 bytes remains one response or becomes multiple fragments.
8. **Repeat each case.** Run at least three complete updates per setting; record median and worst-case duration, retry rate, and failure/recovery behavior.
9. **Promote conservatively.** Ship the largest value that remains reliable across representative coordinators and routes, not merely the fastest single run.

A compact success criterion is: no failed update in repeated runs, no increase in normal-network packet loss, bounded retry rate, and a measurable reduction in median completion time. The test should also verify resume behavior, power interruption recovery, image signature/CRC validation, and correct rejection of an incompatible image.

## Decision on the Current Firmware Change

The coding agent’s caution is justified, but its estimate that the firmware change is inherently speculative is too pessimistic. A 48-to-64-byte ceiling is a worthwhile bench experiment because it can reveal whether the current implementation is unnecessarily constraining the client, and Zigbee2MQTT already exposes a compatible user-side tuning path. However, the change should be isolated behind a compile-time or runtime option, instrumented, and tested with the actual coordinator configuration.

The best immediate sequence is to test Zigbee2MQTT’s delay at 100 ms with the existing b34 firmware, then test the b35 bench build with a 64-byte request while recording actual response sizes. If 64 bytes is consistently returned unfragmented and the response-to-next-request gap is small, the size change is useful. If the server still returns 48–50 bytes, or if the dominant gap is device-side, focus on delay, polling, flash scheduling, or page/delta OTA instead.

The production ceiling fixture should remain on the verified bug-fix build until the bench experiment establishes a measured benefit and acceptable failure behavior. The user-facing configuration should document that `default_maximum_data_size: 64` is optional, coordinator-dependent, and potentially less stable; the firmware should not require every user to change that setting merely to remain compatible.

---

## References

1. [OTA device firmware update](https://www.zigbee2mqtt.io/guide/configuration/ota-device-updates.html) - default_maximum_data_size: 50. OTA Index override file. ota: zigbee_ota_override_index_location: my_...

2. [OTA updates](https://www.zigbee2mqtt.io/guide/usage/ota_updates.html) - Minimum is 10B and maximum is 100B. Some devices will refuse higher sizes than 50/64 bytes. Zigbee2M...

3. [02 Fundamentals | OTA Bootload Server and Client Setup](https://docs.silabs.com/zigbee/8.2.3/ota-bootload-server-client-setup-zigbee-sdk-v7x-higher/02-fundamentals) - Since the OTA image is larger than the maximum payload size of a Zigbee packet, it needs to be broke...

4. [02 Fundamentals | OTA Bootload Server and Client Setup](https://docs.silabs.com/zigbee/latest/ota-bootload-server-client-setup-zigbee-sdk-v7x-higher/02-fundamentals) - Since the OTA image is larger than the maximum payload size of a Zigbee packet, it needs to be broke...

5. [Connectivity:STM32WB Zigbee OTA - stm32mcu - ST wiki](https://wiki.st.com/stm32mcu/wiki/Connectivity:STM32WB_Zigbee_OTA) - The Zigbee protocol typically supports a maximum MTU size of 127 bytes at the physical layer. These ...

6. [14 Sleepy Devices | Zigbee Application Framework ...](https://docs.silabs.com/zigbee/9.0.0/zigbee-app-framework-dev-guide-sdk-7x/14-sleepy-devices) - The SHORT_POLL interval is configurable in one-second increments and may be modified at compile-time...

7. [Z-Stack Overview - http - Texas Instruments](https://software-dl.ti.com/simplelink/esd/plugins/simplelink_zigbee_sdk_plugin/2.20.00.06/exports/docs/zigbee_user_guide/html/zigbee/developing_zigbee_applications/z_stack_developers_guide/z-stack-overview.html) - When APS Fragmentation is turned on, sending a data request with a payload larger than a normal data...

8. [Ancillary Features and Resources for OTA Upgrade](https://mcuxpresso.nxp.com/mcuxsdk/25.09.00/html/middleware/wireless/zigbee/Docs/JNUG3132/OTA_upgrade_cluster/topics/ancillary_features_and_resources_for_ota_upgrade.html) - The server can change the value of the 'block request delay' attribute on the client at any time, ev...

9. [Optimizing OTA Updates with Silicon Labs' Delta DFU ...](https://www.silabs.com/blog/optimizing-ota-updates-with-silicon-labs-delta-dfu-technology) - Delta DFU drastically reduces the size of update files, leading to faster updates and reduced networ...

