#!/usr/bin/env python3
"""
p3i_decode.py -- PilotAware P3I application decoder. Sits on top of the generic
sv650_decode link layer: sv650_decode gives raw de-whitened payload bytes, this file
parses/validates them as P3I. Keep P3I-specific logic here only.

P3I frame (24 bytes, little-endian; full description in docs/P3I_PROTOCOL.md):
  [0]      start: 0x24 (v1) / 0x25 (v2) / 0x48 (status)
  [1:4]    ICAO address (24-bit little-endian)
  [4:8]    longitude  (float32 LE, deg)
  [8:12]   latitude   (float32 LE, deg)
  [12:14]  altitude   (uint16 LE)
  [14:16]  ground speed (int16 LE)
  [16]     emitter type (low nibble)
  [17]     nav flags (low 2 bits)
  [20:22]  track (int16 LE, deg)
  [23]     XOR-8 checksum over bytes [0:23]

v2 frames (0x25) have bytes 1..22 XOR-obfuscated with a time-rotating table; they pass
the checksum but their fields are NOT de-obfuscated here.

Usage (source options are shared with sv650_decode.py):
    python3 p3i_decode.py cap.iq                    # decode P3I from a u8 IQ file
    python3 p3i_decode.py --rtlsdr --secs 30        # local USB RTL-SDR
    python3 p3i_decode.py --tcp 192.168.1.74:1234   # remote rtl_tcp server
    import p3i_decode; frame = p3i_decode.parse(payload_bytes)   # standalone parse
    
Author: Gareth Owenson
"""
__author__ = "Gareth Owenson"

import argparse, struct
import sv650_decode as sv

P3I_START = (0x24, 0x25, 0x48)    # v1 position, v2 position, status/heartbeat


def xor8_ok(w):
    """True if byte 23 equals the XOR of bytes 0..22 (P3I's only integrity check)."""
    x = 0
    for k in range(23):
        x ^= w[k]
    return x == w[23]


def parse(payload):
    """Parse 24 (or more) payload bytes as a P3I frame. Returns dict or None.

    Extra trailing bytes (the SV650 decoder may include CRC/ramp bytes) are ignored.
    Keys: type, icao, raw; status frames add status=True, position frames add
    lat, lng (deg), alt, gs (kt), trk (deg), emitter, nav."""
    if len(payload) < 24:
        return None
    w = payload[:24]
    if w[0] not in P3I_START or not xor8_ok(w):
        return None
    icao = w[1] | (w[2] << 8) | (w[3] << 16)
    d = dict(type=hex(w[0]), icao=f"{icao:06X}", raw=w.hex())
    if w[0] == 0x48:                      # status / heartbeat frame (no position payload)
        d["status"] = True
        return d
    d.update(                              # 0x24/0x25 position report
        lat=round(struct.unpack('<f', w[8:12])[0], 5),
        lng=round(struct.unpack('<f', w[4:8])[0], 5),
        alt=w[12] | (w[13] << 8),
        gs=struct.unpack('<h', w[14:16])[0],
        trk=struct.unpack('<h', w[20:22])[0],
        emitter=w[16] & 0xF, nav=w[17] & 3)
    return d


def decode(packets):
    """Map SV650 packets -> parsed P3I frames (skips non-P3I payloads)."""
    for p in packets:
        f = parse(p.payload)
        if f:
            yield p.t, f


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Decode PilotAware P3I frames from RTL-SDR IQ.")
    sv.add_source_args(ap)
    pkts = sv.packets_from_args(ap.parse_args())
    n = 0
    for t, f in decode(pkts):
        n += 1
        if f.get("status"):
            print(f"t={t*1000:8.1f}ms  ICAO {f['icao']}  STATUS/heartbeat  [{f['type']}]  {f['raw']}")
        else:
            print(f"t={t*1000:8.1f}ms  ICAO {f['icao']}  {f['lat']:.5f},{f['lng']:.5f}  "
                  f"alt={f['alt']}ft gs={f['gs']} trk={f['trk']}  emit={f['emitter']}  [{f['type']}]")
    print(f"# {len(pkts)} SV650 packets, {n} valid P3I frames")
