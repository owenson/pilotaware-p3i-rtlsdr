# p3i — PilotAware P3I receiver for RTL-SDR

*Author: Gareth Owenson*

Decodes PilotAware P3I traffic beacons off-air with a cheap RTL-SDR dongle, without
needing an SV650 radio module.

It is split into two layers:

| File | Layer | What it does |
|---|---|---|
| `sv650_decode.py` | Link layer (generic) | IQ capture -> FSK demodulation -> sync -> de-whitening. Outputs the raw payload bytes that the sender wrote to its NiceRF SV650 radio's UART. It knows nothing about P3I. |
| `p3i_decode.py` | Application layer | Checks and parses those payloads as P3I frames (position / status). |

Protocol documentation:

- [`docs/SV650_LINK_LAYER.md`](docs/SV650_LINK_LAYER.md): RF parameters, framing and the whitening mask
- [`docs/P3I_PROTOCOL.md`](docs/P3I_PROTOCOL.md): the 24-byte P3I frame formats

## Requirements

- Python 3.8+, `numpy`, `scipy`
- An RTL-SDR (R820T/R828D tuner) and one of:
  - **local**: the `rtl_sdr` tool (`apt install rtl-sdr`, or `rtl-sdr` on most distros)
  - **remote**: an `rtl_tcp` server, e.g. on a Raspberry Pi near the antenna

```sh
pip install numpy scipy
```

## Usage

Both scripts take the same source options. Choose exactly one source:

```sh
# local USB dongle (device index 0), 30-second capture
python3 p3i_decode.py --rtlsdr --secs 30

# pick a dongle by index or serial number, set gain, keep the raw IQ
python3 p3i_decode.py --rtlsdr 1 --gain 20 --save cap.iq

# remote rtl_tcp server (default 192.168.1.74:1234)
python3 p3i_decode.py --tcp
python3 p3i_decode.py --tcp 10.0.0.5:1234 --secs 60

# offline, from a saved capture
python3 p3i_decode.py cap.iq

# link layer only: hex dump of every de-whitened SV650 payload (any protocol)
python3 sv650_decode.py --rtlsdr --secs 10
```

| Option | Default | Meaning |
|---|---|---|
| `iqfile` | — | u8 interleaved IQ file at 2.048 Msps, tuned to 869.62 MHz |
| `--rtlsdr [DEV]` | `0` | capture with local `rtl_sdr`; DEV is an index or serial number |
| `--tcp [HOST[:PORT]]` | `192.168.1.74:1234` | capture from `rtl_tcp` |
| `--secs N` | `5` | capture length |
| `--gain DB` | `25` | tuner gain in dB, or `auto` |
| `--ppm N` | `0` | dongle frequency correction |
| `--save FILE` | — | also write the captured IQ to FILE |

Example output:

```
# capturing 3s from rtl_tcp 192.168.1.74:1234 @ 869.92 MHz
t=  1597.7ms  ICAO XXXXXX  NN.NNNNN,-N.NNNNN  alt=39ft gs=0 trk=0  emit=15  [0x24]
# 1 SV650 packets, 1 valid P3I frames
```

To record your own file for offline use:
`rtl_sdr -f 869620000 -s 2048000 -g 25 -n 20480000 cap.iq` (10 s).

### Tips

- The SDR tunes to 869.62 MHz, and the signal sits 300 kHz above that at 869.92 MHz.
  This keeps the burst away from the RTL's DC spike.
- `--gain auto` (tuner AGC) works well at normal range. In live tests it decoded as
  reliably as manual 25 dB. The decoder reads frequency, not amplitude, so gain changes
  don't matter; only clipping does.
- The SV650 transmits at up to 500 mW. Very close to a unit, the AGC may react too slowly
  to the short bursts and the dongle clips. If you see bursts but no frames, use manual
  gain around 10–20 dB.
- Beacons are sparse: roughly one position frame every 1.6 s, and one status frame
  per minute when the unit has no GPS fix. Use `--secs 30` or longer when testing.
- There is a strong, unrelated transmitter at 869.566 MHz. The channel filter rejects it.

## Library use

```python
import sv650_decode as sv, p3i_decode as p3i

pkts = sv.decode_live(secs=10, gain_db=20, source="rtlsdr", device="0")
# or: sv.decode_live(10, source="tcp", host="10.0.0.5", port=1234)
# or: sv.decode_file("cap.iq")

for t, frame in p3i.decode(pkts):
    print(t, frame)          # {'type': '0x24', 'icao': 'XXXXXX', 'lat': ..., ...}

p3i.parse(bytes.fromhex("24..."))   # parse one 24-byte payload directly
```

`sv650_decode.LinkConfig` holds every radio parameter (frequency, bit rate, sync word,
whitening mask). Pass your own instance as `cfg=` if your SV650 is set to a different
channel or RF rate.

## Limitations

- p3i v2 (0x25) frames pass the checksum, but their fields are still obfuscated. The
  time-rotating XOR table is not applied yet.  The decoding table is not supplied here.
- The link-layer constants were measured from the PilotAware SV650 configuration
  (ch 21 / 868 band / RF rate index 6 = 38400 bps). Other configurations will need
  a different `tune` and `bitrate`. The sync word and mask may also differ.
- Captures are processed in fixed windows (`--secs`); there is no continuous streaming mode.
- The trailing CRC the SV650 adds is not checked. Integrity relies on P3I's own XOR checksum.

## License

Apache License 2.0. See [LICENSE](LICENSE).
