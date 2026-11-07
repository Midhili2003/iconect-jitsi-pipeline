#!/usr/bin/env python3
"""plot_traffic.py - audio waveform and encrypted uplink traffic on the same time axis.

Usage:
  /opt/mci-venv/bin/python /opt/iconect/plot_traffic.py RUN_FOLDER [OUT.png] [--window START END] [--slide]

RUN_FOLDER must hold stimulus.wav, uplink.pcap and meta.json from one run_clip.py run.
  OUT.png     where to save the graph (default: RUN_FOLDER/traffic_vs_audio.png)
  --window    only show START..END seconds after the clip starts (e.g. --window 0 25)
  --slide     larger fonts and no title, for slides

How the traffic line is made (nothing is decrypted):
  * only the sender's RTP audio packets to the videobridge (UDP port 10000) are used;
    STUN, RTCP and padding-only bandwidth probes are left out
  * each packet is placed in time by its RTP timestamp (the sender's 48 kHz sampling clock,
    in the unencrypted RTP header), because network arrival times jitter by a few ms
  * packets are summed into 100 ms windows (bytes on the wire per 100 ms)
  * traffic and audio are aligned with the three sync chirps played before the clip
"""
import json, sys
from pathlib import Path

import dpkt
import numpy as np
import soundfile as sf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

BIN = 0.1        # 100 ms windows
PKT = 0.02       # one Opus packet = 20 ms of audio


def packets(pcap, t_play):
    """(time in s after playback start, bytes on the wire) for each RTP audio packet."""
    arr, rtp, size = [], [], []
    with open(pcap, "rb") as f:
        for ts, buf in dpkt.pcap.Reader(f):
            try:
                u = dpkt.ethernet.Ethernet(buf).data.data
            except Exception:
                continue
            if not isinstance(u, dpkt.udp.UDP) or u.dport != 10000:
                continue
            p = bytes(u.data)
            if len(p) < 12 or p[0] >> 6 != 2 or 192 <= p[1] <= 223:   # keep RTP only (no STUN/RTCP)
                continue
            if p[0] & 0x20:                                           # padding bit: probe, no audio
                continue
            arr.append(float(ts) - t_play)
            rtp.append(int.from_bytes(p[4:8], "big"))
            size.append(len(buf))
    if not arr:
        raise SystemExit("no RTP audio packets to port 10000 found in uplink.pcap")
    arr = np.array(arr)
    rtp = np.unwrap(np.array(rtp, float), period=2 ** 32)
    t_rtp = (rtp - rtp[0]) / 48000.0
    offset = np.percentile(arr - t_rtp, 5)     # earliest arrivals = least network delay
    return np.column_stack([t_rtp + offset, size])


def segments(times, active):
    segs = []
    for t, a in zip(times, active):
        if not a:
            continue
        if segs and t - segs[-1][1] <= 0.10:
            segs[-1][1] = t + PKT
        else:
            segs.append([t, t + PKT])
    return [(s, e) for s, e in segs if e - s >= 0.06]


def find_delay(pk, onsets):
    """Delay between playback and network, from the three sync chirps."""
    t, n = pk[:, 0], pk[:, 1]
    quiet = (t >= -8) & (t <= -1)
    base = np.median(n[quiet]) if quiet.any() else np.median(n)
    segs = segments(t, n > base + 20)
    g1, g2 = onsets[1] - onsets[0], onsets[2] - onsets[0]
    for i in range(len(segs) - 2):
        a, b, c = segs[i][0], segs[i + 1][0], segs[i + 2][0]
        if abs((b - a) - g1) < 0.08 and abs((c - a) - g2) < 0.08:
            return a - onsets[0]
    raise SystemExit("sync chirps not found in the packets")


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        sys.exit(__doc__)
    slide = "--slide" in args
    if slide:
        args.remove("--slide")
    win = None
    if "--window" in args:
        i = args.index("--window")
        win = (float(args[i + 1]), float(args[i + 2]))
        del args[i:i + 3]
    run = Path(args[0])
    out = Path(args[1]) if len(args) > 1 else run / "traffic_vs_audio.png"

    meta = json.loads((run / "meta.json").read_text())
    lay = meta["layout"]
    pk = packets(run / "uplink.pcap", meta["t_play_ns"] / 1e9)
    delay = find_delay(pk, lay["marker_onsets_s"])
    c0, c1 = lay["clip_start_s"], lay["clip_end_s"]

    # traffic: seconds since the clip started, binned by 20 ms frame into 100 ms windows
    t_rel = pk[:, 0] - delay - c0
    t_rel = (np.round(t_rel / PKT) + 0.5) * PKT      # centre of each frame: never on a window edge
    edges = np.arange(0, np.floor((c1 - c0) / BIN) * BIN + BIN / 2, BIN)
    bytes_per_bin, _ = np.histogram(t_rel, bins=edges, weights=pk[:, 1])
    centres = edges[:-1] + BIN / 2

    # audio: the clip part of stimulus.wav
    x, sr = sf.read(run / "stimulus.wav", dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    audio = x[int(c0 * sr):int(c1 * sr)]
    ta = np.arange(len(audio)) / sr
    step = max(1, len(audio) // 60000)
    ta, audio = ta[::step], audio[::step]

    # draw the waveform on the bytes axis, centred in the traffic's range
    lo, hi = np.percentile(bytes_per_bin, 1), np.percentile(bytes_per_bin, 99)
    mid, half = (lo + hi) / 2, (hi - lo) / 2
    wave = mid + audio / (np.max(np.abs(audio)) + 1e-9) * half

    if win:
        ka = (ta >= win[0]) & (ta <= win[1])
        kb = (centres >= win[0]) & (centres <= win[1])
        ta, wave, centres, bytes_per_bin = ta[ka], wave[ka], centres[kb], bytes_per_bin[kb]

    if slide:
        plt.rcParams.update({"font.size": 15, "axes.labelsize": 16,
                             "xtick.labelsize": 14, "ytick.labelsize": 14})
    fig, ax = plt.subplots(figsize=(12, 4.6) if slide else (13, 4.2), dpi=200 if slide else 150)
    ax.plot(ta, wave, color="#f0504a", lw=0.5, alpha=0.75, label="Audio waveform")
    ax.plot(centres, bytes_per_bin, color="#1f2fd6", lw=1.6, label="Encrypted voice call traffic")
    ax.set_xlabel("Time since audio start (s)")
    ax.set_ylabel("Upload bytes / 100 ms")
    pad = (hi - lo) * 0.35
    ax.set_ylim(lo - pad, hi + pad * 1.4)
    ax.set_xlim(ta[0], ta[-1])
    ax.grid(color="#e6e6e6", lw=0.6)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.legend(loc="upper right", frameon=False, ncol=2, fontsize=14 if slide else 9)
    if not slide:
        ax.set_title(f"{meta['clip_id']}: audio vs. encrypted Jitsi uplink "
                     f"(network delay {delay * 1000:.0f} ms removed)", fontsize=10, loc="left")
    fig.tight_layout()
    fig.savefig(out)
    print("saved", out, "| delay", round(delay * 1000), "ms")


if __name__ == "__main__":
    main()
