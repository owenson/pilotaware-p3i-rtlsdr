#!/usr/bin/env python3
"""
sv650_decode.py -- generic receiver/decoder for the NiceRF SV650 link layer, from an
RTL-SDR (local USB dongle via `rtl_sdr`, or remote via `rtl_tcp`). Protocol-agnostic: it
recovers the raw payload bytes exactly as they were handed to the radio's UART, with NO
knowledge of what rides on top (P3I, ADS-L, your own protocol, ...). Feed the payload
bytes to a separate application decoder (see p3i_decode.py).

Pipeline:  u8 IQ -> DDC + channel LP -> burst detect -> quadrature FSK demod
           -> fixed-timing bit slice -> sync search -> strip framing -> de-whiten -> bytes

Link-layer parameters below were reverse-engineered by chosen-plaintext from a real SV650
configured as: channel 21 / band 868 (869.92 MHz), RF rate 38400, and are
specific to that module's configuration. Change LinkConfig for a differently-configured unit.
See docs/SV650_LINK_LAYER.md for the full description.

  RF        : 869.92 MHz (2-GFSK, +/-30 kHz deviation)
  bit rate  : 38400 bps (53.333 samples/symbol @ 2.048 Msps)
  frame     : preamble 0101.. | 62-bit sync | payload (<=56 bytes) | ~CRC tail
  whitening : payload XOR fixed 56-byte mask, MSB-first, reset at the start of every packet
              (max radio-packet payload = 56 bytes; longer UART writes fragment into
               multiple packets, each re-using the mask from byte 0).

Library use:
    import sv650_decode as sv
    for pkt in sv.decode_live(secs=5, source="rtlsdr"):   # or source="tcp"
        do_something(pkt.payload)                          # raw de-whitened bytes
    pkts = sv.decode_file("cap.iq")                        # offline

CLI:
    python3 sv650_decode.py cap.iq                         # decode a u8 IQ file
    python3 sv650_decode.py --rtlsdr --secs 10             # local USB dongle (index 0)
    python3 sv650_decode.py --tcp 192.168.1.74:1234        # remote rtl_tcp server
    python3 sv650_decode.py --rtlsdr --save cap.iq         # also keep the raw IQ
    
Author: Gareth Owenson
"""
__author__ = "Gareth Owenson"

import argparse, socket, struct, subprocess, sys, time
from dataclasses import dataclass, field
import numpy as np
from scipy import signal


# ----------------------------- configuration -----------------------------
@dataclass
class LinkConfig:
    """SDR capture settings plus the SV650 link-layer constants."""
    fs:      float = 2_048_000.0        # SDR sample rate (Hz)
    bitrate: float = 38_400.0           # SV650 RF data rate (bps)
    tune:    int   = 869_620_000        # SDR centre frequency (Hz)
    foffset: float = 300e3              # signal offset from tune (signal at tune+foffset),
                                        # keeps the burst clear of the RTL DC spike
    ppm:     int   = 0                  # dongle frequency correction
    host:    str   = "192.168.1.74"     # default rtl_tcp server
    port:    int   = 1234
    # 62-bit framing anchor that immediately precedes the payload
    sync: str = "00101111010100111111111111111111111111111111111110011110001110"
    # 56-byte payload whitening mask (max packet payload); repeats per packet
    mask_hex: str = ("fa4bfa51eb25407c3bfb4dfb29b2781dfe5cd953449c0efe"
                     "35f842509f37ed12fb4309edd3fe26fb4e2afc54f930f719"
                     "0df82fed3df6cbdf")
    max_sync_err: int = 8               # allowed Hamming errors when matching sync

    @property
    def sps(self):
        """Samples per symbol (non-integer; the slicer uses fractional timing)."""
        return self.fs / self.bitrate
    @property
    def sync_bits(self): return np.array([int(c) for c in self.sync], np.uint8)
    @property
    def mask_bits(self): return np.unpackbits(np.frombuffer(bytes.fromhex(self.mask_hex), np.uint8))

DEFAULT = LinkConfig()


@dataclass
class Packet:
    """One decoded SV650 radio packet."""
    t: float                 # seconds from start of capture
    payload: bytes           # de-whitened payload bytes (as written to the radio UART)
    nbits: int               # total demodulated bits in the burst
    sync_pos: int            # bit index where the sync anchor matched
    sync_err: int            # Hamming distance of the sync match
    payload_bits: np.ndarray = field(repr=False, default=None)  # raw de-whitened bits
    tail_bits:    np.ndarray = field(repr=False, default=None)  # trailing bits (CRC/ramp)


# ----------------------------- capture -----------------------------
# All capture functions return interleaved unsigned 8-bit I/Q (the native RTL format,
# also what `rtl_sdr` writes to disk). gain_db=None selects the tuner's AGC.
# The SV650 is loud at short range: keep manual gain around 20-25 dB to avoid clipping.

def capture_tcp(secs, gain_db=25.0, cfg=DEFAULT, host=None, port=None):
    """Capture `secs` seconds of u8 IQ from an rtl_tcp server."""
    s = socket.create_connection((host or cfg.host, port or cfg.port), timeout=5)
    s.recv(12)                                    # dongle info header ("RTL0" + tuner)
    cmds = [(0x02, int(cfg.fs)), (0x01, int(cfg.tune)), (0x05, cfg.ppm), (0x08, 0)]
    if gain_db is None:
        cmds += [(0x03, 0)]                       # gain mode: auto
    else:
        cmds += [(0x03, 1), (0x04, int(round(gain_db * 10)))]   # manual, tenths of dB
    for c, p in cmds:
        s.sendall(struct.pack(">BI", c, p & 0xFFFFFFFF))
    time.sleep(0.2)                               # let the tuner settle
    need = int(cfg.fs * 2 * secs); buf = bytearray(); s.settimeout(10)
    while len(buf) < need:
        chunk = s.recv(min(1 << 20, need - len(buf)))
        if not chunk: break
        buf.extend(chunk)
    s.close()
    return np.frombuffer(bytes(buf), np.uint8)


def capture_rtlsdr(secs, gain_db=25.0, cfg=DEFAULT, device="0"):
    """Capture `secs` seconds of u8 IQ from a locally attached dongle via the `rtl_sdr`
    tool (package rtl-sdr). `device` is a device index or serial string."""
    n = int(cfg.fs * secs)
    cmd = ["rtl_sdr", "-d", str(device), "-f", str(int(cfg.tune)), "-s", str(int(cfg.fs)),
           "-g", "0" if gain_db is None else f"{gain_db:g}", "-p", str(cfg.ppm),
           "-n", str(n), "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=secs + 20)
    except FileNotFoundError:
        raise RuntimeError("rtl_sdr not found -- install the rtl-sdr package") from None
    if len(r.stdout) < 2 * n * 0.9:
        err = r.stderr.decode(errors="replace").strip().splitlines()
        raise RuntimeError("rtl_sdr capture failed: " + (err[-1] if err else f"exit {r.returncode}"))
    return np.frombuffer(r.stdout, np.uint8)


def capture(secs, gain_db=25.0, cfg=DEFAULT, source="tcp", **kw):
    """Capture from `source` = "tcp" (kw: host, port) or "rtlsdr" (kw: device)."""
    if source == "tcp":
        return capture_tcp(secs, gain_db, cfg, **kw)
    if source == "rtlsdr":
        return capture_rtlsdr(secs, gain_db, cfg, **kw)
    raise ValueError(f"unknown source {source!r}")


# ----------------------------- DSP -----------------------------
def _to_complex(raw_u8):
    """Interleaved u8 I/Q -> complex64 in [-1, 1]."""
    raw = raw_u8.astype(np.float32)
    return ((raw[0::2] - 127.5) + 1j * (raw[1::2] - 127.5)) / 127.5

def _channelize(iq, cfg):
    """Mix the signal at tune+foffset down to 0 Hz and low-pass to the ~120 kHz channel."""
    t = np.arange(len(iq)) / cfg.fs
    z = iq * np.exp(-1j * 2 * np.pi * cfg.foffset * t)
    return signal.lfilter(signal.firwin(255, 60e3 / (cfg.fs / 2)), 1.0, z).astype(np.complex64)

def _find_bursts(lp, cfg, min_ms=2.0, snr_db=10):
    """Return (start, end) sample indices of bursts whose smoothed envelope is at least
    `snr_db` above the median (noise floor) for at least `min_ms`."""
    env = np.abs(lp); sm = np.convolve(env, np.ones(128) / 128, 'same')
    thr = np.median(sm) * 10 ** (snr_db / 20); above = sm > thr
    ed = np.diff(above.astype(np.int8)); st = np.where(ed == 1)[0]; en = np.where(ed == -1)[0]
    if len(en) and (len(st) == 0 or en[0] < st[0]): en = en[1:]     # drop a burst cut at t=0
    L = min(len(st), len(en))
    return [(int(a), int(b)) for a, b in zip(st[:L], en[:L]) if (b - a) / cfg.fs * 1000 >= min_ms]

def _demod_bits(lp, a, b, cfg, pad_ms=1.0):
    """FSK-demodulate one burst into hard bits (1 = high tone).

    Quadrature discriminator -> instantaneous frequency; the DC offset (carrier error) is
    removed using the medians of the two tone levels. Symbol timing is fixed at cfg.sps
    and only the sub-symbol phase is searched (32 steps, maximising mean |freq| at the
    sample points). Fixed timing matters: whitened payloads contain long single-tone
    runs that make clock-recovery loops (e.g. GNU Radio symbol_sync) slip.
    """
    sps = cfg.sps
    pad = int(cfg.fs * pad_ms / 1000); seg = lp[max(0, a - pad):b + pad]; env = np.abs(seg)
    inst = np.angle(seg[1:] * np.conj(seg[:-1])) * cfg.fs / (2 * np.pi)
    act = np.where(env[1:] > env.max() * 0.30)[0]              # keyed-on region only
    if len(act) < 200: return None
    fa = inst[act[0]:act[-1]].astype(np.float64)
    hi = np.median(fa[fa > np.median(fa)]); lo = np.median(fa[fa < np.median(fa)])
    fa -= 0.5 * (hi + lo)
    best = None
    for ph in np.linspace(0, sps, 32, endpoint=False):
        N = int((len(fa) - ph) / sps); idx = (ph + np.arange(N) * sps).astype(int); idx = idx[idx < len(fa)]
        m = np.mean(np.abs(fa[idx]))
        if best is None or m > best[0]: best = (m, ph)
    ph = best[1]; N = int((len(fa) - ph) / sps)
    idx = (ph + np.arange(N) * sps).astype(int); idx = idx[idx < len(fa)]
    return (fa[idx] > 0).astype(np.uint8)

def _match_sync(bits, cfg):
    """Slide the sync anchor over `bits`; return (hamming_err, pos) of the best match."""
    sa = cfg.sync_bits; n = len(sa); best = (n + 1, -1)
    for i in range(len(bits) - n):
        e = int((bits[i:i + n] != sa).sum())
        if e < best[0]: best = (e, i)
        if e == 0: break
    return best  # (err, pos)

def _dewhiten(payload_bits, cfg):
    """XOR payload bits with the mask (tiled if ever longer than one mask period)."""
    m = cfg.mask_bits
    if len(payload_bits) > len(m):
        m = np.tile(m, len(payload_bits) // len(m) + 1)
    return (payload_bits ^ m[:len(payload_bits)]).astype(np.uint8)


# ----------------------------- public decode API -----------------------------
def decode_iq(raw_u8, cfg=DEFAULT):
    """Decode raw u8 IQ -> list[Packet].

    Every whole byte after the sync anchor is treated as payload; the SV650 carries no
    length field, so a few trailing bytes of CRC/ramp-down may be included -- the
    application layer knows its own frame length (P3I = 24 bytes)."""
    lp = _channelize(_to_complex(raw_u8), cfg)
    n = len(cfg.sync_bits); out = []
    for a, b in _find_bursts(lp, cfg):
        bits = _demod_bits(lp, a, b, cfg)
        if bits is None: continue
        err, pos = _match_sync(bits, cfg)
        if pos < 0 or err > cfg.max_sync_err: continue
        after = bits[pos + n:]
        nbytes = len(after) // 8
        pay_bits = after[:nbytes * 8]
        clear = _dewhiten(pay_bits, cfg)
        out.append(Packet(
            t=a / cfg.fs, payload=np.packbits(clear).tobytes(), nbits=len(bits),
            sync_pos=pos, sync_err=err, payload_bits=clear, tail_bits=after[nbytes * 8:]))
    return out

def decode_file(path, cfg=DEFAULT):
    """Decode a u8 IQ file (as written by `rtl_sdr` or --save) captured at cfg.tune/cfg.fs."""
    return decode_iq(np.fromfile(path, dtype=np.uint8), cfg)

def decode_live(secs=5, gain_db=25.0, cfg=DEFAULT, source="tcp", **kw):
    """Capture `secs` seconds from `source` ("tcp" or "rtlsdr") and decode it."""
    return decode_iq(capture(secs, gain_db, cfg, source, **kw), cfg)


# ----------------------------- shared CLI -----------------------------
def _gain(s):
    return None if s.lower() == "auto" else float(s)

def add_source_args(ap, cfg=DEFAULT):
    """Add the IQ-source options (file / --rtlsdr / --tcp) to an argparse parser.
    Shared by this script and application decoders such as p3i_decode.py."""
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("iqfile", nargs="?", help="u8 IQ file to decode")
    src.add_argument("--rtlsdr", nargs="?", const="0", metavar="DEV",
                     help="capture from a local RTL-SDR (device index or serial, default 0)")
    src.add_argument("--tcp", nargs="?", const=f"{cfg.host}:{cfg.port}", metavar="HOST[:PORT]",
                     help=f"capture from an rtl_tcp server (default {cfg.host}:{cfg.port})")
    ap.add_argument("--secs", type=float, default=5, help="capture length in seconds (default 5)")
    ap.add_argument("--gain", type=_gain, default=25.0,
                    help="tuner gain in dB, or 'auto' (default 25)")
    ap.add_argument("--ppm", type=int, default=cfg.ppm, help="dongle frequency correction")
    ap.add_argument("--save", metavar="FILE", help="also write the captured IQ to FILE")

def packets_from_args(args, cfg=DEFAULT):
    """Capture/load according to parsed add_source_args() options -> list[Packet]."""
    if args.iqfile:
        return decode_file(args.iqfile, cfg)
    cfg = LinkConfig(**{**cfg.__dict__, "ppm": args.ppm})
    if args.rtlsdr is not None:
        kw, where = dict(source="rtlsdr", device=args.rtlsdr), f"rtl_sdr device {args.rtlsdr}"
    else:
        host, _, port = args.tcp.partition(":")
        kw, where = dict(source="tcp", host=host, port=int(port or cfg.port)), f"rtl_tcp {args.tcp}"
    print(f"# capturing {args.secs:g}s from {where} @ {(cfg.tune + cfg.foffset) / 1e6:.2f} MHz",
          file=sys.stderr)
    try:
        raw = capture(args.secs, args.gain, cfg, **kw)
    except (RuntimeError, OSError) as e:
        sys.exit(f"error: {e}")
    if args.save:
        raw.tofile(args.save)
    return decode_iq(raw, cfg)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Dump de-whitened SV650 payloads from RTL-SDR IQ.")
    add_source_args(ap)
    pkts = packets_from_args(ap.parse_args())
    for p in pkts:
        print(f"t={p.t*1000:8.1f}ms  {len(p.payload):3d}B  syncErr={p.sync_err}  {p.payload.hex()}")
    print(f"# {len(pkts)} SV650 packets")
