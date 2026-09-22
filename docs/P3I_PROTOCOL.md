# P3I frame format

*Author: Gareth Owenson*

P3I is PilotAware's own traffic-awareness protocol. A unit broadcasts its position about
once a second. It is not FLARM, OGN or ADS-B. PilotAware combines those sources in its
daemon, but on air P3I is a separate format. It is carried over the SV650 radio link
described in [SV650_LINK_LAYER.md](SV650_LINK_LAYER.md).

## Common structure

Every P3I frame is 4 bytes. Multi-byte fields are little-endian.

```
byte 0       start byte / frame type
bytes 1..3   ICAO 24-bit address (LE)
bytes 4..22  type-specific body
byte 23      checksum = XOR of bytes 0..22
```

| Byte 0 | ASCII | Type |
|---|---|---|
| `0x24` | `$` | Position, v1 (plaintext) |
| `0x25` | `%` | Position, v2 (body obfuscated, see below) |
| `0x48` | `H` | Status / heartbeat |

Receivers discard any other start byte.

### Checksum

```c
uint8_t acc = 0;
for (int i = 0; i < 23; i++) acc ^= pkt[i];
pkt[23] = acc;
```

This is a plain 8-bit XOR parity, not a CRC. About 1 in 256 random corruptions passes it,
and it misses reordered bytes and any even number of flips in the same bit column. In p3i v2
the checksum is computed after obfuscation, so v2 frames can be checked without the key.

## Position frame (`0x24` / `0x25`)

Built by `p3iGetOwnship()`.

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 1 | start byte | `0x24` v1, `0x25` v2 |
| 1 | 3 | ICAO | 24-bit LE |
| 4 | 4 | longitude | float32, degrees (**longitude before latitude**) |
| 8 | 4 | latitude | float32, degrees |
| 12 | 2 | altitude | uint16. The firmware computes `gps_alt * 0.3048`, and the receiver rejects values ≥ 6000. The live frames we captured suggest **metres** (see below). |
| 14 | 2 | ground speed | int16, knots |
| 16 | 1 | emitter category | bits 0–3. `0xD`/`0xE` in this nibble mark a rebroadcast (see below). Bits 4–7 are reserved. |
| 17 | 1 | nav state | bits 0–1 = nav state 0–3, bit 2 = "no track" (set 60 s after the last good GPS fix), bits 3–7 reserved |
| 18 | 2 | reserved | 0 |
| 20 | 2 | track | int16, degrees |
| 22 | 1 | reserved / ACK marker | `0xFF` in ACK frames, 0 otherwise |
| 23 | 1 | checksum | XOR of bytes 0..22 |

In anonymous mode the ICAO is `0xFF0001` and the nav state is forced to 0.

An ACK frame has the same envelope, so byte 0 is still `0x24`/`0x25`. Its body is
zeroed except for the acknowledged aircraft's ICAO in bytes 1–3 and `0xFF` at byte 22.
Units send ACKs only when `volatile_tx_ack=1` is set.

### Rebroadcast frames (SKYGRID)

A ground station in uplink mode retransmits other aircraft using the same position envelope
(`getYourship()`). The position-owner's ICAO stays in bytes 1–3, and bytes 16–17 are
overlaid:

- byte 16, bits 0–3: `0xE` = downlink rebroadcast, `0xD` = uplink rebroadcast (0 = direct)
- bytes 16–17, upper bits: the low 16 bits of the relay station's own ICAO
- byte 17, bits 0–6: age of the source entry

For source entries whose emitter category is `0xE`, the producer swaps latitude and longitude.

## Status / heartbeat frame (`0x48`)

Built by `getStatus()`, sent about once every 60 s.

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | `0x48` |
| 1 | 3 | own ICAO |
| 4 | 3 | 24 bits of the licence MAC |
| 7 | 1 | — |
| 8 | 2 | compact date, int16: `year*372 + (month-1)*31 + (day-1)`, counted from 2020 |
| 10 | 1 | bit 0 = uplink station, bit 1 = uplink RF power high |
| 11 | 1 | bit 0 = barometer present; bits 1–5 = signal quality `round((x-20)/2.5)`; bit 6 = ADS-B receiver ready; bit 7 = Mode-S receiver ready |
| 12 | 1 | bit 0 = UAT (978) ready; bit 1 = FLARM ready; bits 2–5 = SV650 firmware minor version |
| 13 | 8 | detector-state bitmap |
| 21 | 1 | 0 |
| 22 | 1 | reserved |
| 23 | 1 | checksum |

`p3i_decode.py` only checks and labels status frames. It does not decode their fields.

## v2 obfuscation (`0x25`)

In v2 mode (selected by the GRID server's `ENCRYPT <n>` or by local config), bytes 1–22 are
XORed with a 22-byte slice of a fixed 8170-byte table (`xorBody()`). This table is not supplied here.

```python
hour   = unix_time // 3600
offset = (hour * 22) % 8170
for i in range(22):
    pkt[1 + i] ^= table[offset + i]
```

- The key changes every UTC hour. Because gcd(22, 8170) = 2, the sequence repeats after
  4085 hours, about 170 days.
- There is no secret key: anyone with the table and a roughly correct clock can undo it.
  If a frame arrives near an hour boundary, try both neighbouring hours.

## Receiver-side checks (`p3iStaticCheckOK`)

PilotAware drops position frames that fail any of these checks. They are useful for
spotting mis-decodes:

| Field | Accepted |
|---|---|
| ICAO | ≠ 0 |
| longitude | −180 … 180, not NaN |
| latitude | −90 … 90, not NaN |
| altitude | < 6000 |
| speed | < 400 kt |
| distance from receiver | < 100 (NM, probably) |

(The daemon's "track < 360" check reads offset 14, which is ground speed. This looks like a
bug in PilotAware.)

## Transmit cadence

| Frame | Interval |
|---|---|
| Own position | 1.6 s ± 0.4 s jitter, only with a valid GPS fix |
| Status | 60 s |
| Rebroadcast | as each relayed entry falls due (uplink stations only) |


