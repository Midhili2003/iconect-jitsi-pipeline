#!/usr/bin/env python3
"""pause_zoom.py - look closely at the longest marked pauses of one run:
loudness of the played audio next to the encrypted packet sizes, every 40 ms.

Usage:
  /opt/mci-venv/bin/python /opt/iconect/pause_zoom.py RUN_FOLDER LABELS.txt [N]
  (N = how many of the longest pauses to show, default 3)
"""
import json, sys
from pathlib import Path

import dpkt
import numpy as np
import soundfile as sf

STEP = 0.04


def packets(run, t_play):
    out = []
    with open(run / "uplink.pcap", "rb") as f:
        for ts, buf in dpkt.pcap.Reader(f):
            try:
                u = dpkt.ethernet.Ethernet(buf).data.data
            except Exception:
                continue
            if not isinstance(u, dpkt.udp.UDP) or u.dport != 10000:
                continue
            p = bytes(u.data)
            if len(p) < 12 or p[0] >> 6 != 2 or 192 <= p[1] <= 223:
                continue
            out.append((float(ts) - t_play, len(u)))
    return np.array(out)


def main():
    run, labels = Path(sys.argv[1]), Path(sys.argv[2])
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    meta = json.loads((run / "meta.json").read_text())
    feats = json.loads((run / "features.json").read_text())
    c0 = meta["layout"]["clip_start_s"]
    delay = feats["delay_ms"] / 1000
    th = feats["threshold_bytes"]
    x, sr = sf.read(run / "stimulus.wav", dtype="float32")
    pk = packets(run, meta["t_play_ns"] / 1e9)
    marks = []
    for line in open(labels):
        p = line.split("\t")
        if len(p) >= 2 and not line.startswith("\\"):
            marks.append((float(p[0]), float(p[1])))
    marks.sort(key=lambda m: m[0] - m[1])
    print(f"{run.parent.name}: packet line (threshold) = {th} bytes; delay {delay * 1000:.0f} ms")
    for s, e in marks[:n]:
        print(f"\n--- marked pause {s:.2f}-{e:.2f} s (length {e - s:.2f} s); times in clip seconds ---")
        print(f"{'time':>7} {'audio dB':>9} {'packet bytes':>13}   ({'*' * 3} = above the line = counted as speech)")
        for t in np.arange(s - 0.4, e + 0.2, STEP):
            a = x[int((c0 + t) * sr):int((c0 + t + STEP) * sr)]
            db = 20 * np.log10(np.sqrt(np.mean(a.astype(np.float64) ** 2)) + 1e-9) if a.size else -99
            sel = pk[(pk[:, 0] - delay >= c0 + t) & (pk[:, 0] - delay < c0 + t + STEP), 1] if len(pk) else []
            size = float(np.mean(sel)) if len(sel) else float("nan")
            mark = "|" if s <= t < e else " "
            flag = "***" if size > th else ""
            print(f"{t:7.2f} {mark}{db:8.1f} {size:13.0f}   {flag}")


if __name__ == "__main__":
    main()
