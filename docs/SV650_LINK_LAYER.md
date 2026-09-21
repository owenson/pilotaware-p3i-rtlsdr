# NiceRF SV650 over-the-air link layer

*Author: Gareth Owenson*

The SV650 is a 500 mW sub-GHz UART radio module built around a Si4463-class transceiver.
The host writes bytes to its UART in "working" mode, and the module adds framing and
whitening before transmitting them. A receiving SV650 undoes both and passes the original
bytes to its own host. PilotAware uses one to carry P3I.

The vendor does not document the on-air format. Everything here was **reverse-engineered by
chosen plaintext** (2026-09-21): a bench SV650 was made to transmit known payloads
(all-zeros, all-ones, ramps, impulses, `deadbeef…`, real P3I frames, 48- and
256-byte blocks) while an RTL-SDR recorded them. The format is fully deterministic:
the same input always produces the same bits on air.

`sv650_decode.py` implements everything in this document.

## Module configuration measured

These values apply to the configuration below. Other channels or RF rates will change at
least the frequency and bit rate.

Parameter block read with `AA FA 01 00` (CMD_RD_PAR):
`15 03 06 02 07 02 01 01 00 00 00 00 00 00`

| Byte | Value | Meaning |
|---|---|---|
| 0 | `0x15` | channel 21 |
| 1 | `0x03` | band 868 (channels 1 MHz apart, starting at 849.92 MHz) |
| 2 | `0x06` | RF rate index 6 = **38 400 bps** |
| 3 | `0x02` | TX power index 2 |
| 4 | `0x07` | UART 57 600 baud |
| 5–7 | `02 01 01` | 8 data bits, 1 stop bit, no parity |
| 8–11 | `00…` | network ID 0 |
| 12–13 | `00 00` | node ID 0 |

Channel 21 in the 868 band = 849.92 + 20 × 1 MHz = **869.92 MHz**. Real PilotAware units
use the same channel.

The parameter block has no fields for the sync word, whitening or CRC. These are fixed
inside the SV650 firmware.

## Physical layer

| Property | Value |
|---|---|
| Carrier | 869.920 MHz |
| Modulation | 2-GFSK, about ±30 kHz deviation (tones measured near +26 / −30 kHz) |
| Bit rate | 38 400 bps (53.333 samples/bit at 2.048 Msps) |
| Bit mapping | high tone = 1 |
| Burst length | 348 bits ≈ 9.1 ms for a 24-byte payload |

Two corrections to earlier assumptions:

- The "community" figures of 100 kbps and ±50 kHz are wrong for this configuration.
- A strong image appears at 868.5 MHz when the RTL is tuned near it. It looks like a
  19 200 bps signal with scrambled payloads. It is an artefact: only about 1 in 40 packets
  appears there, versus about 90% at 869.92 MHz.

### Receiver notes

- Tune the SDR 300 kHz low (869.62 MHz) so the burst sits clear of the DC spike.
- Demodulate with a **fixed** symbol period and only a sub-bit phase search. Whitened
  payloads can contain long runs of one tone, and PLL-style clock recovery (e.g. GNU Radio
  `symbol_sync`) slips on them. Demodulating at half the bit rate gives similar-looking
  garbage.
- Remove the carrier-frequency offset using the midpoint of the two tone medians.

## Frame structure

```
| preamble      | sync / framing anchor | payload (whitened)  | tail    |
| 82 bits 0101… | 62 bits               | 8·N bits, N ≤ 56    | ~12 bits|
```

**Preamble.** Alternating `0101…` starting with 0, about 82 bits.

**Sync anchor.** These 62 bits come immediately before the first payload bit:

```
00101111010100111111111111111111111111111111111110011110001110
```

The decoder accepts up to 8 bit errors when matching it (`LinkConfig.max_sync_err`).
Do not use the value 0x23A7DCA8 that appears in older notes: it came from the 868.5 MHz image.

**Payload.** N bytes, sent **MSB first**, whitened as described below. No length field
has been seen on air. The decoder takes every whole byte after the sync anchor, so it
usually returns one extra byte of tail (e.g. 25 bytes for a 24-byte P3I frame). The
application layer knows its own frame length.

**Tail.** About 12 bits after the payload, followed by the power ramp-down. These are
presumably a CRC, but it has not been identified and the decoder does not check it.

## Whitening

The payload is XORed bit by bit with a **fixed 56-byte mask**, MSB first:

```
fa4bfa51eb25407c 3bfb4dfb29b2781d fe5cd953449c0efe 35f842509f37ed12
fb4309edd3fe26fb 4e2afc54f930f719 0df82fed3df6cbdf
```

```
air_bit[i] = payload_bit[i] XOR mask_bit[i]      (i from 0 at the first payload bit)
```

How it was established:

- An all-zero input transmits the mask itself. XORing the all-zero and all-ones outputs
  gives all ones across the payload, so the transform is a pure XOR that does not depend
  on the data.
- This is not the standard Si446x PN9 sequence, and no LFSR is involved. Berlekamp–Massey
  gives a linear complexity of about half the length at every length tested
  (101 at 192 bits, 193 at 384 bits). Treat it as a fixed table.
- **The mask restarts for every radio packet.** The largest radio packet carries 56 bytes.
  A longer UART write is split into several 56-byte packets plus a remainder, and each one
  is whitened from mask byte 0. For example, a 256-byte write goes out as 56+56+56+56+32.
  A mask longer than 56 bytes therefore never occurs. `_dewhiten` tiles the mask only as a
  safety measure.

P3I frames are 24 bytes, so they only use the first 24 mask bytes.

## Reassembling longer writes

Each radio packet is decoded on its own. Rebuilding a UART write longer than 56 bytes
means joining consecutive packets in time order. This project does not need that, because
P3I is always a single 24-byte packet.

## Timeline of findings

| Date | Finding |
|---|---|
| 2026-09-20 | First decoding attempts, at 868.5 MHz, 19 200 bps and sync 0x23A7DCA8. All of these later turned out to belong to an image. |
| 2026-09-21 | SV650 parameter block read. Channel 21 = 869.92 MHz, RF rate 38 400 bps. |
| 2026-09-21 | Fixed-timing demodulator. Chosen-plaintext runs recovered the 62-bit sync and the 56-byte mask. |
| 2026-09-21 | End-to-end check: 15/15 P3I frames from the bench unit byte-perfect. First live frame (a status frame) received from a running PilotAware unit. |
