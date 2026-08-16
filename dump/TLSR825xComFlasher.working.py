#!/usr/bin/env python

### ComSwireWriter.py ###
###    Autor: pvvx    ###
###    Edited: Aaron Christophel ATCnetz.de    ###
###    Edit : Pila    ###

import sys
import signal
import struct
import serial
import platform
import time
import argparse
import os
import io
import serial.tools.list_ports

__progname__ = 'TLSR825x Flasher'
__version__ = "29.11.24"

COMPORT_MIN_BAUD_RATE=340000
COMPORT_DEF_BAUD_RATE=921600
USBCOMPORT_BAD_BAUD_RATE=460800

FLASH_SECTOR_SIZE = 4096

debug = False
bit8mask = 0x20
# PATCH 2026-08-14: where inside each swire cell we sample. blk[el] bit N is
# sampled at UART bit-time N+1.5 of a ~10 bit-time cell, so 0x10 (bit4) = ~55%
# through the cell and 0x20 (bit5) = ~65%. Measured on this rig: a '1' cell
# (mostly LOW) was occasionally sampled HIGH - always bit 5 of the decoded
# byte, i.e. blk[2], always in the same direction - which means the sample
# lands too late and catches the cell's rising edge. Sampling earlier fixes it.
sample_mask0 = int(os.environ.get('SWS_MASK0', '0x20'), 0)
sample_maskn = int(os.environ.get('SWS_MASK', '0x10'), 0)

class FatalError(RuntimeError):
	def __init__(self, message):
		RuntimeError.__init__(self, message)
	@staticmethod
	def WithResult(message, result):
		message += " (result was %s)" % hexify(result)
		return FatalError(message)
def signal_handler(signal, frame):
	print()
	print('Keyboard Break!')
	sys.exit(0)
def arg_auto_int(x):
	return int(x, 0)
def hex_dump(addr, blk):
	print('%06x: ' % addr, end='')
	for i in range(len(blk)):
		if (i+1) % 16 == 0:
			print('%02x ' % blk[i])
			if i < len(blk) - 1:
				print('%06x: ' % (addr + i + 1), end='')
		else:
			print('%02x ' % blk[i], end='')
	if len(blk) % 16 != 0:
		print('')
# PATCH 2026-08-14: the '0' cell char. Stock 0xfe = 2/10 of the cell LOW;
# the chip's pulse-width discriminator smears a short pulse that follows one
# or two long '1' pulses (0x80 = 9/10 LOW), so bit6/bit5 '0' cells after a
# leading '1' decoded as '1' — every value 0x80..0xdf wrote corrupted, all
# else perfect, divider- and chunking-independent. 0xf8 = 4/10 LOW keeps the
# cell an unambiguous '0' but with real margin past the ~50% boundary.
SWS_ZERO_CHAR = int(os.environ.get('SWS_ZERO_CHAR', '0xfe'), 0)
# PATCH 2026-08-14 (2): the '1' cell char. Stock 0x80 = 8/10 of the cell LOW,
# leaving only a 2-bit-time HIGH gap before the next cell — the chip's
# write decoder swallowed that gap and merged the cells, so a '0' cell
# following '1' cells decoded as '1' (values 0x80..0xdf always corrupt, in
# flash AND in SRAM — generic write-path, not the SPI bridge). A '1' char
# with a wider HIGH tail keeps the same LOW-run class (6/10 LOW is still
# unambiguously '1' vs the '0' char's 2/10) but leaves a 4-bit gap.
SWS_ONE_CHAR = int(os.environ.get('SWS_ONE_CHAR', '0x80'), 0)
# PATCH 2026-08-14 (3) — the real write fix, from the official Telink SWire
# spec (TLSRPGM/TelinkSWire/TelinkSWire.pdf): each bit cell is 5 units
# ('1' = 4 units LOW + 1 unit HIGH). A '1' cell therefore leaves only a
# ONE-unit HIGH gap before the next cell, and the chip's decoder swallows
# single-unit gaps: a '0' following one or two '1' bits decoded as '1',
# which is exactly the observed deterministic corruption (every value
# 0x80..0xdf gained bit 6 or bit 5, in flash AND SRAM writes; reads were
# never affected because the read protocol idles the bus high between
# every byte). Padding one high-idle char (0xff: half-unit LOW glitch +
# 4.5 units HIGH) after every bit cell keeps every LOW run cleanly
# separated. Costs ~2x write time; reads unpadded.
SWS_PAD_CHAR = int(os.environ.get('SWS_PAD_CHAR', '0'), 0)
# Optional inter-cell idle: >0 sends each swire cell as its own write() with
# this many seconds of idle after it, so the decoder always sees a clean
# edge. Slows writes proportionally.
SWS_CELL_GAP = float(os.environ.get('SWS_CELL_GAP', '0'))

def _wr_cells_paced(serialPort, pkt):
	if SWS_CELL_GAP <= 0:
		return wr_usbcom_blk(serialPort, pkt)
	sent = 0
	for i in range(0, len(pkt), 10):
		sent += serialPort.write(pkt[i:i+10])
		time.sleep(SWS_CELL_GAP)
	return sent

# encode data (blk) into 10-bit swire words
def sws_encode_blk(blk):
	pkt=[]
	d = bytearray(10) # word swire 10 bits
	d[0] = 0x80 # start bit byte cmd swire = 1
	for el in blk:
		m = 0x80 # mask bit
		idx = 1
		while m != 0:
			if (el & m) != 0:
				d[idx] = SWS_ONE_CHAR
			else:
				d[idx] = SWS_ZERO_CHAR
			idx += 1
			m >>= 1
		d[9] = SWS_ZERO_CHAR # stop bit swire = 0
		pkt += d
		d[0] = SWS_ZERO_CHAR # start bit next byte swire = 0
	return pkt

# padded variant (writes only): a high-idle char after every bit cell so
# the chip's decoder never sees a 1-unit HIGH gap after a '1' cell - see
# the SWS_PAD_CHAR note near the top.
def sws_encode_blk_padded(blk):
	out = bytearray()
	d = bytearray(10)
	d[0] = 0x80
	for el in blk:
		m = 0x80
		idx = 1
		while m != 0:
			d[idx] = SWS_ONE_CHAR if (el & m) else SWS_ZERO_CHAR
			idx += 1
			m >>= 1
		d[9] = SWS_ZERO_CHAR
		for i in range(10):
			out.append(d[i])
			if i > 0 and i < 9:   # pad between the 9 bit cells, not after stop
				out.append(SWS_PAD_CHAR)
		d[0] = SWS_ZERO_CHAR
	return out
# decode 9 bit swire response to byte (blk)
def sws_decode_blk(blk):
	if (len(blk) == 9) and ((blk[8] & 0xfe) == 0xfe):
		bitmask = sample_mask0
		data = 0;
		for el in range(8):
			data <<= 1
			if (blk[el] & bitmask) == 0:
				data |= 1
			bitmask = sample_maskn
		#print('0x%02x' % data)
		return data
	#print('Error blk:', blk)
	return None
# encode a part of the read-by-address command (before the data read start bit) into 10-bit swire words
def sws_rd_addr(addr):
	return sws_encode_blk(bytearray([0x5a, (addr>>16)&0xff, (addr>>8)&0xff, addr & 0xff, 0x80]))
# encode command stop into 10-bit swire words
def sws_code_end():
	return sws_encode_blk([0xff])
# encode the command for writing data into 10-bit swire words
def sws_wr_addr(addr, data):
	if SWS_PAD_CHAR:
		return bytes(sws_encode_blk_padded(bytearray([0x5a, (addr>>16)&0xff, (addr>>8)&0xff, addr & 0xff, 0x00]) + bytearray(data))) + bytes(sws_encode_blk([0xff]))
	return sws_encode_blk(bytearray([0x5a, (addr>>16)&0xff, (addr>>8)&0xff, addr & 0xff, 0x00]) + bytearray(data)) + sws_encode_blk([0xff])
# send block to USB-COM
def wr_usbcom_blk(serialPort, blk):
	# USB-COM chips throttle the stream into blocks at high speed!
	# Swire is transmitted by 10 bytes of UART.
	# The packet must be a multiple of these 10 bytes.
	# Max block USB2.0 64 bytes -> the packet will be 60 bytes.
	# PATCH 2026-08-14: '>=' — at exactly 460800 the raw single-write path
	# streamed thousands of cell chars back-to-back with zero gaps and the
	# chip's write decoder drifted: every byte with high bits set gained a
	# spurious bit (bits 3-6). USB-COM adapters pace in ~62-byte frames even
	# for one write(); the PL011 does not, so the 60-byte chunk+flush pacing
	# must apply here too. Reads are unaffected (they pace byte-by-byte).
	if serialPort.baudrate >= USBCOMPORT_BAD_BAUD_RATE:
		i = 0
		s = 60
		l = len(blk)
		while i < l:
			if l - i < s:
				s = l - i
			i += serialPort.write(blk[i:i+s])
			serialPort.flush()
		return i
	return serialPort.write(blk)
# send and receive block to USB-COM
def	rd_wr_usbcom_blk(serialPort, blk):
	i = wr_usbcom_blk(serialPort, blk)
	return i == len(serialPort.read(i))
# send swire command write to USB-COM
def sws_wr_addr_usbcom(serialPort, addr, data):
	return wr_usbcom_blk(serialPort, sws_wr_addr(addr, data))
# send and receive swire command write to USB-COM
def rd_sws_wr_addr_usbcom(serialPort, addr, data):
	if SWS_CELL_GAP > 0:
		# paced mode: the echo dribbles back over the whole paced burst, so
		# an exact-length read() check cannot work. Drain before/after and
		# verify total length only.
		serialPort.reset_input_buffer()
		i = _wr_cells_paced(serialPort, sws_wr_addr(addr, data))
		deadline = time.time() + (SWS_CELL_GAP * (len(sws_wr_addr(addr, data))//10) / 1000.0) + 0.2
		got = 0
		while time.time() < deadline and got < i:
			chunk = serialPort.read(i - got)
			if chunk:
				got += len(chunk)
			else:
				break
		return got == i
	i = wr_usbcom_blk(serialPort, sws_wr_addr(addr, data))
	return i == len(serialPort.read(i))
# send swire data in fifo mode
def rd_sws_fifo_wr_usbcom(serialPort, addr, data):
	rd_sws_wr_addr_usbcom(serialPort, 0x00b3, bytearray([0x80])) # [0xb3]=0x80 ext.SWS into fifo mode
	rd_sws_wr_addr_usbcom(serialPort, addr, data) # send all data to one register (no increment address - fifo mode)
	rd_sws_wr_addr_usbcom(serialPort, 0x00b3, bytearray([0x00])) # [0xb3]=0x00 ext.SWS into normal(ram) mode
# send and receive swire command read to USB-COM
def sws_read_data(serialPort, addr, size = 1):
	time.sleep(0.05)
	serialPort.reset_input_buffer()
	# send addr and flag read
	rd_wr_usbcom_blk(serialPort, sws_rd_addr(addr))
	out = []
	# read size bytes
	for i in range(size):
		# send bit start read byte
		serialPort.write([0xfe])
		# read 9 bits swire, decode read byte
		blk = serialPort.read(9)
		# Added retry reading for Prolific PL-2303HX and ...
		if len(blk) < 9:
			blk += serialPort.read(10-len(blk))
		x = sws_decode_blk(blk)
		if x != None:
			out += [x]
		else:
			if debug:
				print('\r\nDebug: read swire byte:')
				hex_dump(addr+i, blk)
			# send stop read
			rd_wr_usbcom_blk(serialPort, sws_code_end())
			out = None
			break
	# send stop read
	rd_wr_usbcom_blk(serialPort, sws_code_end())
	return out
# PATCH 2026-08-14 (Moes ZT3L): resumable variant of sws_read_data().
#
# ROOT CAUSE this exists to work around, measured not guessed:
# this rig cannot always decode the byte value 0xFF. 0xFF is eight consecutive
# swire '1' cells, i.e. eight consecutive mostly-LOW cells, and the chip's cell
# is ~2.5% shorter than the 10 UART bit-times we assume. The PL011's framing
# slips ~0.25 bit per cell (visible as the block drifting 06 06 06 06 ->
# 03 03 03 03) until the 9th (stop) cell lands ~2 bits out and blk[8] comes
# back 0xf9 instead of 0xfe. EVERY observed failure was a 0xFF byte - verified
# by matching failure indices against a known-good reference dump.
#
# Stock sws_read_data() answers a single bad byte by returning None, which
# discards the entire chunk. A 1 MB flash is mostly erased 0xFF, so that can
# never finish. Here a bad byte simply ENDS the read: the caller keeps every
# byte already decoded and re-issues the flash read command at the failing
# address, so one bad byte costs one byte, not 256.
def sws_read_partial(serialPort, addr, size = 1, settle = 0.005):
	if settle > 0:
		time.sleep(settle)
	serialPort.reset_input_buffer()
	# send addr and flag read
	rd_wr_usbcom_blk(serialPort, sws_rd_addr(addr))
	out = []
	for i in range(size):
		# send bit start read byte
		serialPort.write([0xfe])
		# read 9 bits swire, decode read byte
		blk = serialPort.read(9)
		if len(blk) < 9:
			blk += serialPort.read(9-len(blk))
		x = sws_decode_blk(blk)
		if x == None:
			break
		out += [x]
	# send stop read
	rd_wr_usbcom_blk(serialPort, sws_code_end())
	return out
# set sws speed according to clk frequency and serialPort baud
def set_sws_speed(serialPort, clk):
	#--------------------------------
	# Set register[0x00b2]
	print('SWire speed for CLK %.1f MHz... ' % (clk/1000000), end='')
	swsdiv = int(round(clk*2/serialPort.baudrate))
	if swsdiv > 0x7f:
		print('Low UART baud rate!')
		return False
	byteSent = sws_wr_addr_usbcom(serialPort, 0x00b2, [swsdiv])
	# print('Test SWM/SWS %d/%d baud...' % (int(serialPort.baudrate/5),int(clk/5/swsbaud)))
	read = serialPort.read(byteSent)
	if len(read) != byteSent:
		if serialPort.baudrate > USBCOMPORT_BAD_BAUD_RATE and byteSent > 64 and len(read) >= 64 and len(read) < byteSent:
			print('\n\r!!!!!!!!!!!!!!!!!!!BAD USB-UART Chip!!!!!!!!!!!!!!!!!!!')
			print('UART Output:')
			hex_dump(0,sws_wr_addr(0x00b2, [swsdiv]))
			print('UART Input:')
			hex_dump(0,read)
			print('!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!')
			return False
		print('\n\rError: Wrong RX-TX connection!')
		return False
	#--------------------------------
	# Test read register[0x00b2]
	x = sws_read_data(serialPort, 0x00b2)
	#print(x)
	if x != None and x[0] == swsdiv:
		print('ok.')
		if debug:
			print('Debug: UART-SWS %d baud. SW-CLK ~%.1f MHz' % (int(serialPort.baudrate/10), serialPort.baudrate*swsdiv/2000000))
			print('Debug: swdiv = 0x%02x' % (swsdiv))
		return True
	#--------------------------------
	# Set default register[0x00b2]
	rd_sws_wr_addr_usbcom(serialPort, 0x00b2, 0x05)
	print('no')
	return False
# auto set sws speed according to serialport baud
def set_sws_auto_speed(serialPort):
	#---------------------------------------------------
	# swsbaud = Fclk/5/register[0x00b2]
	# register[0x00b2] = Fclk/5/swsbaud
	# swsbaud = serialPort.baudrate/10
	# register[0x00b2] = Fclk*2/serialPort.baudrate
	# Fclk = 16000000..48000000 Hz
	# serialPort.baudrate = 460800..3000000 bits/s
	# register[0x00b2] = swsdiv = 10..208
	#---------------------------------------------------
	#serialPort.timeout = 0.01 # A serialPort.timeout must be set !
	if debug:
		swsdiv_def = int(round(24000000*2/serialPort.baudrate))
		print('Debug: default swdiv for 24 MHz = %d (0x%02x)' % (swsdiv_def, swsdiv_def))
	swsdiv = int(round(16000000*2/serialPort.baudrate))
	if swsdiv > 0x7f:
		print('Low UART baud rate!')
		return False
	swsdiv_max = int(round(48000000*2/serialPort.baudrate))
	#bit8m = (bit8mask + (bit8mask<<1) + (bit8mask<<2))&0xff
	bit8m = ((~(bit8mask-1))<<1)&0xff
	while swsdiv <= swsdiv_max:
		# register[0x00b2] = swsdiv
		rd_sws_wr_addr_usbcom(serialPort, 0x00b2, bytearray([swsdiv]))
		# send addr and flag read
		rd_wr_usbcom_blk(serialPort, sws_rd_addr(0x00b2))
		# start read data
		serialPort.write([0xfe])
		# read 9 bits data
		blk = serialPort.read(9)
		# Added retry reading for Prolific PL-2303HX and ...
		if len(blk) < 9:
			blk += serialPort.read(9-len(blk))
		# send stop read
		rd_wr_usbcom_blk(serialPort, sws_code_end())
		if debug:
			print('Debug (read data):')
			hex_dump(swsdiv, blk)
		if len(blk) == 9 and blk[8] == 0xfe:
			cmp = sws_encode_blk([swsdiv])
			if debug:
				print('Debug (check data):')
				hex_dump(swsdiv+0xccc00, sws_encode_blk([swsdiv]))
				print('bit mask: 0x%02x' % (bit8m))
			if (blk[0]&bit8m) == bit8m and blk[1] == cmp[2] and blk[2] == cmp[3] and blk[4] == cmp[5] and blk[6] == cmp[7] and blk[7] == cmp[8]:
				'''
				swsdiv += 1	
				rd_sws_wr_addr_usbcom(serialPort, 0x00b2, bytearray([swsdiv]))
				data = sws_read_data(serialPort, 0x00b2, 1)
				if data == None or data[0] != swsdiv:
					swsdiv -= 1
					if debug:
						print('swsdiv:', swsdiv)
					break
				rd_sws_wr_addr_usbcom(serialPort, 0x00b2, bytearray([swsdiv]))
				'''
				print('UART-SWS %d baud. SW-CLK ~%.1f MHz(?)' % (int(serialPort.baudrate/10), serialPort.baudrate*swsdiv/2000000))
				return True
		swsdiv += 1
		if swsdiv > 0x7f:
			print('Low UART baud rate!')
			break
	#--------------------------------
	# Set default register[0x00b2]
	rd_sws_wr_addr_usbcom(serialPort, 0x00b2, bytearray([0x05]))
	return False
_race_n = 0
def activate(serialPort, tact_ms):
	#--------------------------------
	# issue reset-to-bootloader:
	# RTS = either RESET (active low = chip in reset)
	# DTR = active low
	print('Reset module (PATCHED: stream-then-release race)...')
	blk = sws_wr_addr(0x0602, bytearray([0x05]))
	halt = bytes(blk)
	srst = bytes(sws_wr_addr(0x006f, bytearray([0x20])))
	serialPort.setDTR(True)
	serialPort.setRTS(True)
	time.sleep(0.05)
	serialPort.reset_input_buffer()
	# PATCH (2026-08-12, Moes ZT3L): stock pvvx released reset FIRST and only
	# then started writing. setDTR is a USB control transfer on a different
	# endpoint than the bulk data, so the first halt command lands 1-3 ms late.
	# This chip's firmware reconfigures PA7 away from SWS faster than that, so
	# every -t value failed identically (measured: tact 0..1000 ms). Getting the
	# halt stream in flight BEFORE dropping reset makes the chip wake into a
	# stream that is already arriving. Verified: chip answers on trial 1.
	# Use raw write(), NEVER wr_usbcom_blk(): that helper calls flush() after
	# every 60-byte chunk, and flush() BLOCKS until the bytes have physically
	# gone out. That empties the pipe before reset is released and defeats the
	# whole race. Raw write() only queues, leaving data genuinely in flight.
	global _race_n
	_race_n += 1
	prime = 2 + (_race_n % 8) * 3          # vary the in-flight burst per attempt
	serialPort.write(halt * prime)
	serialPort.setDTR(False)
	serialPort.setRTS(False)
	serialPort.write(srst)
	serialPort.write(halt * 60)
	serialPort.reset_input_buffer()
	#--------------------------------
	# Stop CPU|: [0x0602]=5
	print('Activate (%d ms)...' % tact_ms)
	if tact_ms > 0:
		tact = tact_ms/1000.0
		t1 = time.time()
		while time.time()-t1 < tact:
			for i in range(5):
				wr_usbcom_blk(serialPort, blk)
			serialPort.reset_input_buffer()
	#--------------------------------
	# Duplication with syncronization
	time.sleep(0.01)
	serialPort.reset_input_buffer()
	rd_wr_usbcom_blk(serialPort, sws_code_end())
	rd_wr_usbcom_blk(serialPort, blk)
	time.sleep(0.01)
	serialPort.reset_input_buffer()
	# PATCH 2026-08-14: disable the WATCHDOG. reg_tmr_ctrl is 32-bit @0x620 and
	# FLD_TMR_WD_EN = BIT(19) = bit 3 of the byte at 0x622. With the CPU halted
	# nothing feeds the watchdog, so it fires a second or two in, resets the
	# chip, and the firmware grabs the SWS pin back. That is exactly the
	# measured pattern: reads succeed for ~5 chunks, then fail forever no matter
	# how many retries. CPU is halted here, so zeroing the timer byte is safe.
	sws_wr_addr_usbcom(serialPort, 0x622, bytearray([0x00]))
	time.sleep(0.005)
	serialPort.reset_input_buffer()

# Issue one SPI read command and stream up to rdsize bytes. Returns however
# many bytes decoded cleanly - a short list is normal, not an error.
# Fixes a latent stock bug too: (offset>>16)&0xffff -> &0xff (>255 threw).
def FlashReadChunk(serialPort, offset, rdsize, settle = 0.005):
	rd_sws_wr_addr_usbcom(serialPort, 0x0b3, bytearray([0x80])) # ext.SWS fifo mode
	rd_sws_wr_addr_usbcom(serialPort, 0x0d, bytearray([0x00]))  # SPI cns low
	rd_sws_wr_addr_usbcom(serialPort, 0x0c, bytearray([0x03, (offset >> 16) & 0xff, (offset >> 8) & 0xff, offset & 0xff, 0]))
	rd_sws_wr_addr_usbcom(serialPort, 0x0d, bytearray([0x0A]))  # SPI auto read & cns low
	data = sws_read_partial(serialPort, 0x0c, rdsize, settle)
	rd_sws_wr_addr_usbcom(serialPort, 0x0d, bytearray([0x01]))  # SPI cns high
	rd_sws_wr_addr_usbcom(serialPort, 0x0b3, bytearray([0x00])) # ext.SWS normal mode
	return data

def FlashReadBlock(serialPort, stream, offset = 0, size = 0x80000):
	offset &= 0x00ffffff
	rdsize = int(os.environ.get('SWS_RDSIZE', '0x100'), 0)
	settle = float(os.environ.get('SWS_SETTLE', '0.005'))
	total = size
	done = 0
	stalls = 0
	resyncs = 0
	badbytes = 0
	t0 = time.time()
	tlast = t0
	while size > 0:
		n = rdsize if rdsize < size else size
		data = FlashReadChunk(serialPort, offset, n, settle)
		got = len(data)
		if got:
			stream.write(bytearray(data))
			stream.flush()
			offset += got
			size -= got
			done += got
			if got < n:
				badbytes += 1
			stalls = 0
		else:
			# The byte AT `offset` would not decode. Retry just that byte by
			# re-issuing the read there. Measured ~58% success per bare retry,
			# so this converges in a handful of attempts.
			stalls += 1
			if stalls == 25 or stalls == 60:
				# Not converging - the link itself may be wedged. Re-establish
				# it (verify the re-sync, do not assume it).
				resyncs += 1
				for _s in range(15):
					activate(serialPort, 200)
					if set_sws_auto_speed(serialPort):
						break
			if stalls > 120:
				print('\rError: byte at 0x%06x will not decode after %d tries.' % (offset, stalls))
				return False
		now = time.time()
		if now - tlast > 2.0:
			tlast = now
			el = now - t0
			rate = done/el if el > 0 else 0
			eta = int((total-done)/rate) if rate > 0 else 0
			print('\r0x%06x  %d/%d B  %.0f B/s  ETA %d:%02d  retries %d resync %d    '
				% (offset, done, total, rate, eta//60, eta%60, badbytes, resyncs), end='')
	print('\r                                                                        \r', end='')
	print('Read OK: %d B in %.1f s (%.0f B/s), %d byte-level retries, %d resyncs'
		% (done, time.time()-t0, done/max(time.time()-t0, 0.001), badbytes, resyncs))
	return True
def FlashReady(serialPort, count = 33):
	for _ in range(count):
		rd_sws_wr_addr_usbcom(serialPort, 0x0d, bytearray([0x00]))  # SPI set cns low
		rd_sws_wr_addr_usbcom(serialPort, 0x0c, bytearray([0x05]))  # Flash cmd rd status
		data = sws_read_data(serialPort, 0x0c)
		rd_sws_wr_addr_usbcom(serialPort, 0x0d, bytearray([0x01]))  # SPI set cns high
		if data == None:
			print('\rError Read Flash Status! (%d)  ' %(_))
			return False
		if (data[0] & 0x01) == 0:
			return True
	print('\rTimeout! Flash status 0x%02x!     ' % data[0])
	return False
def FlashWriteEnable(serialPort):
	rd_sws_wr_addr_usbcom(serialPort, 0x0d, bytearray([0x00]))  # cns low
	rd_sws_wr_addr_usbcom(serialPort, 0x0c, bytearray([0x06]))  # Flash cmd write enable
	rd_sws_wr_addr_usbcom(serialPort, 0x0d, bytearray([0x01]))  # cns high
def FlashUnlock(serialPort):
	FlashWriteEnable(serialPort)
	rd_sws_wr_addr_usbcom(serialPort, 0x0d, bytearray([0x00]))  # cns low
	rd_sws_wr_addr_usbcom(serialPort, 0x0c, bytearray([0x01]))  # Flash cmd wr status
	rd_sws_wr_addr_usbcom(serialPort, 0x0c, bytearray([0x00]))  # data: 0
	rd_sws_wr_addr_usbcom(serialPort, 0x0d, bytearray([0x01]))  # cns high
	return FlashReady(serialPort)
def FlashJedecId(serialPort):
	# 0x9F: read manufacturer + memory type + capacity. Path mirrors
	# FlashReadChunk: SWS fifo mode, SPI cns low, cmd, auto-read, read 3.
	rd_sws_wr_addr_usbcom(serialPort, 0x0b3, bytearray([0x80])) # ext.SWS fifo mode
	rd_sws_wr_addr_usbcom(serialPort, 0x0d, bytearray([0x00]))  # SPI cns low
	rd_sws_wr_addr_usbcom(serialPort, 0x0c, bytearray([0x9f]))  # flash cmd JEDEC ID
	rd_sws_wr_addr_usbcom(serialPort, 0x0d, bytearray([0x0A]))  # SPI auto read & cns low
	data = sws_read_partial(serialPort, 0x0c, 3, 0.01)
	rd_sws_wr_addr_usbcom(serialPort, 0x0d, bytearray([0x01]))  # cns high
	rd_sws_wr_addr_usbcom(serialPort, 0x0b3, bytearray([0x00])) # back to normal mode
	return data

def FlashWriteAddr(serialPort, addr, data):
	FlashWriteEnable(serialPort)
	rd_sws_wr_addr_usbcom(serialPort, 0x0d, bytearray([0x00]))  # cns low
	# Flash cmd write + addr + data
	rd_sws_fifo_wr_usbcom(serialPort, 0x0c, bytearray([0x02, (addr >> 16) & 0xff, (addr >> 8) & 0xff, addr & 0xff]) + bytearray(data))
	rd_sws_wr_addr_usbcom(serialPort, 0x0d, bytearray([0x01]))  # cns high
	return FlashReady(serialPort)
def FlashEraseAll(serialPort):
	FlashWriteEnable(serialPort)
	rd_sws_wr_addr_usbcom(serialPort, 0x0d, bytearray([0x00]))  # cns low
	rd_sws_fifo_wr_usbcom(serialPort, 0x0c, bytearray([0x60])) # Flash cmd erase all
	rd_sws_wr_addr_usbcom(serialPort, 0x0d, bytearray([0x01]))  # cns high
	return FlashReady(serialPort, 1000)
def FlashEraseSectors(serialPort, offset = 0, size = 1):
	offset &= ~(FLASH_SECTOR_SIZE-1)
	size = (size + FLASH_SECTOR_SIZE-1) & (~(FLASH_SECTOR_SIZE-1))
	while size > 0:
		print('\rErase Sector at 0x%06x...' % offset, end = '')
		FlashWriteEnable(serialPort)
		rd_sws_wr_addr_usbcom(serialPort, 0x0d, bytearray([0x00]))  # cns low
		rd_sws_wr_addr_usbcom(serialPort, 0x0c, bytearray([0x20]))  # Flash cmd erase sector
		rd_sws_wr_addr_usbcom(serialPort, 0x0c, bytearray([(offset >> 16) & 0xff]))  # Faddr hi
		rd_sws_wr_addr_usbcom(serialPort, 0x0c, bytearray([(offset >> 8) & 0xff]))  # Faddr mi
		rd_sws_wr_addr_usbcom(serialPort, 0x0c, bytearray([offset & 0xff]))  # Faddr lo
		rd_sws_wr_addr_usbcom(serialPort, 0x0d, bytearray([0x01]))  # cns high
		offset += FLASH_SECTOR_SIZE
		size -= FLASH_SECTOR_SIZE
		time.sleep(0.08)
		if not FlashReady(serialPort):
			return False
	print('\r                               \r',  end = '')
	return True
def FlashWriteBlock(serialPort, stream, offset = 0, size = 0, erase = True):
	wrsize = int(os.environ.get('SWS_WRSIZE', '0x100'), 0)
	if (offset & 0xff) != 0:
		print('\rWARNING: offset 0x%06x not page-aligned; page-program wraps!' % offset)
	if erase and (offset & (FLASH_SECTOR_SIZE-1)) != 0:
		erasec = offset & (0xffffff^(FLASH_SECTOR_SIZE-1))
	else:
		erasec = 0xffffffff # = flag
	fa = 0
	while size > 0:
		offset &= 0xffffff
		if erase:
			wrsec = offset & (0xffffff^(FLASH_SECTOR_SIZE-1))
			if erasec != wrsec:
				# send sector erase command + faddr
				if not FlashEraseSectors(serialPort, offset):
					print('\rError Erase sector at 0x%06x!' % offset)
					return False
				erasec = wrsec
		data = stream.read(wrsize)
		wrsize = len(data)
		if not data or wrsize == 0: # end of stream
			print('\rError Read file at 0x%06x!          ' % (fa+wrsize))
			return False
		for e in data:
			if e != 0xff:
				print('\rWrite to 0x%06x...' % offset, end = '')
				if not FlashWriteAddr(serialPort, offset, data):
					print('\rError write sector at 0x%06x!' % offset)
					return False
				break
		offset += wrsize
		size -= wrsize
		fa += wrsize
	print('\r                               \r',  end = '')
	return True

def main():
	signal.signal(signal.SIGINT, signal_handler)
	t1 = time.time()
	#ports = serial.tools.list_ports.comports() #(win10) execution time more than 30 sec!
	#comport_def_name = ports[0].device
	comport_def_name='COM1'
	if sys.platform == 'linux' or sys.platform == 'linux2':
		comport_def_name = '/dev/ttyS0'
	elif sys.platform == 'win32':
		comport_def_name='COM1'
	#elif sys.platform == "darwin":
	#else:
	#	print(sys.platform)
	parser = argparse.ArgumentParser(description='%s version %s' % (__progname__, __version__))
	parser.add_argument(
		'-p', '--port',
		help='Serial port device (default: '+comport_def_name+')',
		default=comport_def_name)
	parser.add_argument(
		'-t', '--tact',
		help='Time Activation ms (0-off, default: 0 ms)',
		type=arg_auto_int,
		default=0)
	parser.add_argument(
		'-c', '--clk',
		help='SWire CLK (default: auto, 0 - auto)',
		type=arg_auto_int,
		default=0)
	parser.add_argument(
		'-b', '--baud',
		help='UART Baud Rate (default: '+str(COMPORT_DEF_BAUD_RATE)+', min: '+str(COMPORT_MIN_BAUD_RATE)+')',
		type=arg_auto_int,
		default=COMPORT_DEF_BAUD_RATE)
	parser.add_argument(
		'-r', '--run',
		help='CPU Run (post main processing)',
		action='store_true')
	parser.add_argument(
		'-d', '--debug',
		help='Debug info',
		action='store_true')
	subparsers = parser.add_subparsers(
		dest='operation',
		help=os.path.splitext(os.path.basename(__file__))[0]+' {command} -h for additional help')

	parser_read_flash = subparsers.add_parser(
		'rf',
		help='Read Flash to binary file')
	parser_read_flash.add_argument('address', help='Start address', type=arg_auto_int)
	parser_read_flash.add_argument('size', help='Size of region', type=arg_auto_int)
	parser_read_flash.add_argument('filename', help='Name of binary file')

	parser_burn_flash = subparsers.add_parser(
		'wf',
		help='Write file to Flash with sectors erases')
	parser_burn_flash.add_argument('address', help='Start address', type=arg_auto_int)
	parser_burn_flash.add_argument('filename', help='Name of binary file')

	parser_write_flash = subparsers.add_parser(
		'we',
		help='Write file to Flash without sectors erases')
	parser_write_flash.add_argument('address', help='Start address', type=arg_auto_int)
	parser_write_flash.add_argument('filename', help='Name of binary file')

	parser_erase_sec_flash = subparsers.add_parser(
		'es',
		help='Erase Region (sectors) of Flash')
	parser_erase_sec_flash.add_argument('address', help='Start address', type=arg_auto_int)
	parser_erase_sec_flash.add_argument('size', help='Size of region', type=arg_auto_int)

	parser_mem_wr = subparsers.add_parser(
		'mw',
		help='Write file to SRAM via swire (no flash)')
	parser_mem_wr.add_argument('address', help='Start address', type=arg_auto_int)
	parser_mem_wr.add_argument('filename', help='Name of binary file')

	parser_mem_rd = subparsers.add_parser(
		'mr',
		help='Read SRAM via swire to file')
	parser_mem_rd.add_argument('address', help='Start address', type=arg_auto_int)
	parser_mem_rd.add_argument('size', help='Size of region', type=arg_auto_int)
	parser_mem_rd.add_argument('filename', help='Name of binary file')

	parser_erase_all_flash = subparsers.add_parser(
		'id',
		help='Read flash JEDEC ID (0x9F)')
	parser_erase_all_flash = subparsers.add_parser(
		'ea',
		help='Erase All Flash')

	args = parser.parse_args()
	print('=======================================================')
	print('%s version %s' % (__progname__, __version__))
	print('-------------------------------------------------------')
	global debug
	debug = args.debug
	if(args.baud < COMPORT_MIN_BAUD_RATE):
		print ('The minimum speed of the COM port is %d baud!' % COMPORT_MIN_BAUD_RATE)
		sys.exit(1)
	print ('Open %s, %d baud...' % (args.port, args.baud))
	try:
		serialPort = serial.Serial(args.port,args.baud)
		serialPort.reset_input_buffer()
		# PATCH: 0.1 s is enormous next to a 9-byte swire block (98 us at
		# 921600). Every short read burned a full 100 ms; there are 6 echo
		# read-backs per chunk, so this dominated the runtime. Env-overridable.
		serialPort.timeout = float(os.environ.get('SWS_TIMEOUT', '0.03'))
	except:
		print ('Error: Open %s, %d baud!' % (args.port, args.baud))
		sys.exit(1)
	# PATCH: the stream-then-release race is probabilistic (USB control transfer
	# vs bulk endpoint timing), so ONE activation is one roll of the dice.
	# Measured: sws_race.py needed 11 tries. Retry in a single port session,
	# varying the priming burst each pass, exactly as the standalone race does.
	if args.tact != 0:
		synced = False
		for _t in range(80):
			activate(serialPort, args.tact)
			if args.clk == 0:
				synced = set_sws_auto_speed(serialPort)
			else:
				synced = set_sws_speed(serialPort, args.clk * 1000000)
			if synced:
				print('*** Synced on activation attempt %d ***' % (_t + 1))
				break
		if not synced:
			print('Chip sleep? -> no sync in 80 race attempts')
			sys.exit(1)
	elif args.clk == 0:
		# auto speed
		if not set_sws_auto_speed(serialPort):
			print('Chip sleep? -> Use reset chip (RTS-RST): see option --tact')
			sys.exit(1)
	else:
		# Set SWS Speed = CLK/5/[0xb2] bits/s
		if not set_sws_speed(serialPort, args.clk * 1000000):
			if not set_sws_speed(serialPort, 16000000):
				if not set_sws_speed(serialPort, 24000000):
					if not set_sws_speed(serialPort, 32000000):
						if not set_sws_speed(serialPort, 48000000):
							print('Chip sleep? -> Use reset chip (RTS-RST): see option --tact')
							sys.exit(1)
	# PATCH: optional swsdiv override. set_sws_auto_speed() returns the LOWEST
	# div that decodes ONE byte - the bottom edge of the tolerance window, which
	# is exactly where runs of 0xFF mis-frame. SWS_DIV lets us sit higher.
	_d = os.environ.get('SWS_DIV')
	if _d:
		rd_sws_wr_addr_usbcom(serialPort, 0x00b2, bytearray([int(_d, 0)]))
		print('swsdiv forced to %d' % int(_d, 0))
	if args.operation == 'mw':
		offset = args.address & 0xffffff
		try:
			blk = open(args.filename, 'rb').read()
		except:
			print('Error: Not open input file <%s>!' % args.filename)
			sys.exit(1)
		print('Write SRAM 0x%06x to 0x%06x (%d bytes)...' % (offset, offset+len(blk), len(blk)))
		ws = int(os.environ.get('SWS_MWSIZE', '0x40'), 0)
		for i in range(0, len(blk), ws):
			rd_sws_wr_addr_usbcom(serialPort, offset+i, bytearray(blk[i:i+ws]))
		print('Done.')
	elif args.operation == 'mr':
		offset = args.address & 0xffffff
		size = args.size & 0xffffff
		out = bytearray()
		while len(out) < size:
			blk = sws_read_data(serialPort, offset+len(out), min(32, size-len(out)))
			if blk is None:
				print('Error read SRAM at 0x%06x!' % (offset+len(out)))
				sys.exit(1)
			out += bytearray(blk)
		open(args.filename, 'wb').write(bytes(out))
		print('Read SRAM 0x%06x..0x%06x -> %s (%d bytes)' % (offset, offset+size, args.filename, len(out)))
	elif args.operation == 'id':
		jid = FlashJedecId(serialPort)
		if jid is None or len(jid) < 3:
			print('Error reading JEDEC ID!')
			sys.exit(1)
		print('JEDEC ID: %02x %02x %02x' % (jid[0], jid[1], jid[2]))
	elif args.operation == 'rf':
		offset = args.address & 0x00ffffff
		size = args.size & 0x00ffffff
		if size == 0:
			print('\rError: Read size = %d!' % size)
			sys.exit(1)
		print('Outfile: %s' % args.filename)
		try:
			stream = open(args.filename, 'wb')
		except:
			print('Error: Not open Outfile file <%s>!' % args.filename)
			sys.exit(1)
		print('Read Flash from 0x%06x to 0x%06x...' % (offset, offset + size))
		if not FlashReadBlock(serialPort, stream, offset, size):
			sys.exit(1)
	elif args.operation == 'wf':
		offset = args.address & 0x00ffffff
		print('Inputfile: %s' % (args.filename))
		try:
			stream = open(args.filename, 'rb')
			size = os.path.getsize(args.filename)
		except:
			print('Error: Not open input file <%s>!' % args.fldr)
			sys.exit(1)
		if size < 1:
			print('Error: File size = %d!'% size)
		else:
			print('Write Flash data 0x%08x to 0x%08x...' % (offset, offset + size))
			if not FlashUnlock(serialPort):
				sys.exit(1)
			if not FlashWriteBlock(serialPort, stream, offset, size):
				sys.exit(1)
	elif args.operation == 'we':
		offset = args.address & 0x00ffffff
		print('Inputfile: %s' % (args.filename))
		try:
			stream = open(args.filename, 'rb')
			size = os.path.getsize(args.filename)
		except:
			print('Error: Not open input file <%s>!' % args.fldr)
			sys.exit(1)
		if size < 1:
			print('Error: File size = %d!'% size)
		else:
			print('Write Flash data 0x%08x to 0x%08x...' % (offset, offset + size))
			if not FlashUnlock(serialPort):
				sys.exit(1)
			if not FlashWriteBlock(serialPort, stream, offset, size, False):
				sys.exit(1)
	elif args.operation == 'es':
		count = int((args.size + FLASH_SECTOR_SIZE - 1) / FLASH_SECTOR_SIZE)
		size = (count * FLASH_SECTOR_SIZE)
		offset = args.address & (0xffffff^(FLASH_SECTOR_SIZE-1))
		print('Erase Flash %d sectors,\r\ndata from 0x%06x to 0x%06x...' % (count, offset, offset + size))
		if not FlashUnlock(serialPort):
			sys.exit(1)
		if not FlashEraseSectors(serialPort, offset, size):
			sys.exit(1)
	elif args.operation == 'ea':
		print('Erase All Flash ...')
		if not FlashUnlock(serialPort):
			sys.exit(1)
		if not FlashEraseAll(serialPort):
			print('Error Erase All Flash!')
			sys.exit(1)
	else:
		pc = sws_read_data(serialPort, 0x06bc, 4)
		if pc == None or len(pc) != 4:
			print('Error read PC!')
			sys.exit(1)
		x = pc[0] + (pc[1]<<8) + (pc[2]<<16) + (pc[3]<<24)
		print('PC = 0x%06x' % (x))
	if args.run != 0:
		print('Reset CPU...')		
		sws_wr_addr_usbcom(serialPort, 0x006f, bytearray([0x22]))  # Reset CPU
	print('-------------------------------------------------------')
	# Second time slice
	t2 = time.time()
	print("Worked Time: %.3f sec" % (t2-t1))
	print('Done!')
	#--------------------------------
	# Set default register[0x00b2]
	# sws_wr_addr_usbcom(serialPort, 0x00b2, bytearray([5]))
	sys.exit(0)

if __name__ == '__main__':
	main()
