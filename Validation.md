## Verdict

**I would not approve this as “fully verified” or roll it out fleet-wide yet.** The 26→48 neighbour-table change is technically well motivated and probably useful in your dense mesh, but the audit misses one deterministic resource interaction that can reduce broadcast-relay capacity.

I would approve **48 as a canary build**, after adding stronger guards and testing the event-buffer issue below. There is currently no evidence supporting changes to the routing table, address map, rejoin, MAC or APS parameters—but calling those alternatives “placebos” is too strong.

Also, without the exact repository, SDK archive, library, ELF and map files, I cannot independently reproduce the reported hashes, symbol sizes or disassembly. A public Telink SDK generation corroborates the architecture: router neighbour default 26, child limit 16, six fixed additional entries, a trailing neighbour array, runtime size globals, and compiled allocation in `zb_config.c`. It also corroborates the bare address-map/routing defines. ([raw.githubusercontent.com](https://raw.githubusercontent.com/Xinyuan-LilyGO/telink-zigbee-sdk/refs/heads/master/tl_zigbee_sdk/zigbee/nwk/includes/nwk_neighbor.h))

## The main missed problem: event-buffer capacity

Using the audit’s own exact numbers:

| Configuration | Passive-ACK allocation | Smallest fitting class | Eligible buffers | Effective broadcast capacity |
|---|---:|---:|---:|---:|
| 26 neighbours | 52 bytes | 60-byte class | 8 + 8 + 2 = **18** | table-limited to **12** |
| 48 neighbours | 96 bytes | 152-byte class | 8 + 2 = **10** | buffer-limited to **≤10** |

The allocator scans upward and falls back to a larger available class; `ev_buf_getFreeMaxSize()` reports the largest currently available payload. That behaviour is corroborated by public Telink source, although the public SDK mirror has different exact class constants from your archive, so your binaries remain the source of truth. ([raw.githubusercontent.com](https://raw.githubusercontent.com/Xinyuan-LilyGO/telink-zigbee-sdk/refs/heads/master/tl_zigbee_sdk/proj/os/ev_buffer.c))

Consequences:

1. **The 12-entry broadcast transaction table can no longer actually support 12 simultaneous entries.** Its passive-ACK buffers top out at ten, even if no other code is using those pools.
2. The 144-byte link-status scratch buffer shares those same ten eligible buffers.
3. Other NWK/APS traffic will reduce the real capacity below ten.
4. If the 152- and 512-byte pools are busy, `tl_zbNwkSendLinkStatus` can be forced to truncate its neighbour list to whatever smaller class remains.
5. The claimed sustainable rate changes from approximately `12 / 6 = 2` broadcasts/s to at best `10 / 6 ≈ 1.67` broadcasts/s, before contention.
6. This cost occurs **even in a sparse deployment**, because the passive-ACK allocation uses the configured table size, not actual occupancy.

Therefore these conclusions in the audit are not closed:

- “ev_buf pool sizes are not needed at 48”
- “the broadcast table remains capable of 12 transactions”
- “the change does not widen the allocation class”

The link-status allocation remains in the same class, but the passive-ACK allocation moves from the 60-byte class into the 152-byte class.

### Recommended correction

Add a compile-time invariant calculating how many buffers can satisfy:

```c
2 * TL_ZB_NEIGHBOR_TABLE_NUM
```

It should require at least `NWK_BRC_TRANSTBL_NUM + 1` eligible buffers: twelve broadcasts plus one link-status operation, preferably with additional measured margin.

With the stated pools:

- Raising group-2 from 8→10 would let twelve passive-ACK lists exist only by consuming both 512-byte buffers.
- 8→11 also allows one link-status buffer, but still consumes both 512-byte buffers.
- **8→13 is the first sensible baseline** that supports twelve passive-ACK lists plus one link-status list without consuming the 512-byte buffers.
- Five additional 152-byte blocks cost approximately **760 bytes of BSS**, which appears affordable given the reported static headroom, though runtime stack high-water still needs measurement.

If you do not want to alter the pools, I would keep 48 as a **dense-mesh opt-in**, not a universal default.

## Other claims needing correction or qualification

### 1. Multi-frame link status is not yet proven

The Zigbee specification requires more than first/last flags. Entries must be sorted by network address, and the final address in frame N must be repeated as the first address in frame N+1. Link-status frames also require the source IEEE address. ([csa-iot.org](https://csa-iot.org/wp-content/uploads/2023/04/05-3474-23-csg-zigbee-specification-compressed.pdf))

A `Mgmt_Lqi_req` showing more than 26 local entries does **not** verify outbound multi-frame link-status compliance. You need a decrypted packet capture confirming:

- first frame: first=1, last=0;
- second frame: first=0, last=1;
- maximum 26 entries per frame;
- ascending address order;
- mandatory boundary overlap;
- all eligible router neighbours included;
- no truncation under concurrent broadcast load.

It is externally corroborated that increasing beyond 26 may require a second periodic link-status packet, so the cost also includes added RF airtime and processing, not merely 792 bytes of RAM. ([mcuxpresso.nxp.com](https://mcuxpresso.nxp.com/mcuxsdk/25.09.00/html/middleware/wireless/zigbee/Docs/JNUG3130/topics/table_configuration_guidelines.html))

### 2. The routing table is not necessarily a “placebo”

“Entry creation never returns NULL” only proves that allocation succeeds by eviction. It does not prove that eviction is harmless. Repeated eviction can cause route rediscovery, latency and additional RREQ broadcasts.

Keep it at 48 unless measurements show churn, but change the verdict to:

> No evidence presently justifies increasing it.

Also verify from disassembly that the LRU pass explicitly protects the many-to-one concentrator entry, rather than merely not incrementing its `forgetCnt`.

### 3. Address-map demand is not proven to equal current device count

The stated bound `R + E + 1` assumes old mappings are reliably removed. The audit does not demonstrate every deletion path, particularly for:

- below-threshold link-status senders added to the address map but never admitted to the neighbour table;
- departed/replaced devices that were never local neighbours;
- mappings retained because of bindings.

Its high-water may therefore reflect **distinct devices seen since reset**, not merely current network size. Keeping 128 is reasonable, but instrument `validNum` and failed adds before calling it verified adequate.

### 4. “Only NWK table that can saturate” is too broad

The defensible wording is:

> The neighbour table is the persistent topology table most likely to saturate from local router density.

The route-discovery and broadcast tables can still saturate under route-repair or application-broadcast bursts. Generic Zigbee stack guidance explicitly treats broadcast and route-discovery capacity as workload-dependent. ([mcuxpresso.nxp.com](https://mcuxpresso.nxp.com/mcuxsdk/25.09.00/html/middleware/wireless/zigbee/Docs/JNUG3130/topics/table_configuration_guidelines.html))

### 5. Rejoin wording is too confident

The values can remain unchanged, but:

- With a maximum 90-second backoff, recovery after the network returns is not guaranteed “under a minute.”
- A stored-network boot may avoid a scan storm, but simultaneous parent announcements, startup link-status frames and other broadcasts can still produce a boot-time burst.
- “Flashable as-is” should be changed to “build and image format verified”; bootability remains bench-unverified.

### 6. ABI hazards should be enforced, not merely documented

Since the report says changing these values would cause silent corruption, add zero-cost compile-time checks for at least:

```c
sizeof(tl_zb_normal_neighbor_entry_t) == 36
__builtin_offsetof(tl_zb_neighbor_entry_t, freeHead) == 168
__builtin_offsetof(tl_zb_neighbor_entry_t, neighborTbl) == 180
TL_ZB_ADDITION_NEIGHBOR_TABLE_SIZE == 6
```

Put them in a configuration-check translation unit guaranteed to build alongside `zb_config.c`, rather than only in application code. Pinning the SDK/library hash in CI would also prevent an SDK update from silently invalidating the disassembly assumptions.

## Minimum validation before rollout

1. **Idle neighbour test**
   - Use targeted paginated `Mgmt_Lqi_req`.
   - Capture at least ten link-status periods.
   - Compare 26 and 48 builds for occupancy, churn, two-way-cost retention and eviction—not just total count.

2. **On-air link-status test**
   - Verify flags, sorting, boundary overlap and completeness with a sniffer.
   - Repeat while the 152-byte pool is under pressure.

3. **Broadcast relay test**
   - The test light must be an actual intermediate relay to a downstream device; observing the test light itself apply the command is insufficient.
   - Sweep 1, 2 and 4 broadcasts/s, then bursts of 8, 10, 12 and 13 within the six-second window.
   - Count relay frames, passive-ACK retries, allocation failures and downstream delivery.
   - The proposed “20 recalls in two seconds” exceeds both the table and calculated pool capacity, so drops are expected; it is useful as destructive stress, not as the primary pass/fail test.

4. **Power-cycle and soak**
   - Simultaneously restore a circuit of lights with the coordinator already running.
   - Repeat with the coordinator starting late.
   - Run a 24–72 hour canary soak including OTA activity, group commands and route repairs.
   - Record reset reason, exception code, buffer high-water and stack high-water. Device-announcement cadence alone is not a reliable reset detector.

5. **Coordinator health**
   - Run Zigbee2MQTT’s TI-only `coordinator_check` and ensure no routers are missing from coordinator memory. Avoid repeatedly using the full network map during stress tests because Zigbee2MQTT warns that it temporarily reduces network responsiveness. ([zigbee2mqtt.io](https://www.zigbee2mqtt.io/guide/usage/mqtt_topics_and_messages.html))
   - Confirm the actual installed coordinator firmware has source routing/concentrator support rather than assuming it from the adapter model. Common CC2652-class Z-Stack builds expose normal/source-route capacity, but the exact flashed SLZB-06P7 image matters. ([raw.githubusercontent.com](https://raw.githubusercontent.com/Koenkk/Z-Stack-firmware/master/coordinator/README.md))

## Final recommendation

**Keep 48 as the candidate value.** Once the 26-entry boundary is crossed, any value above 26 already pushes the passive-ACK list into the larger class, so 48 sensibly extracts the most neighbour capacity before the link-status list moves into the 512-byte class.

Before broad deployment:

1. add the ABI assertions;
2. add the event-pool capacity assertion and instrumentation;
3. likely enlarge the 152-byte pool if retaining a 12-entry broadcast table;
4. verify multi-frame link status on-air;
5. use a uniquely identifiable canary build.

After those are done, I see no evidence yet for changing the routing table, address map, MAC/APS settings or rejoin cadence. The main remaining improvement is **closing the buffer-pool regression**, not tuning another routing knob.
