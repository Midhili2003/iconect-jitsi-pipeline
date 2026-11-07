#!/usr/bin/env python3
"""extract_pauses.py - turn each run's encrypted packets into speech/pause timings.

Usage:
  /opt/mci-venv/bin/python /opt/iconect/extract_pauses.py RUN_FOLDER [RUN_FOLDER ...]
  /opt/mci-venv/bin/python /opt/iconect/extract_pauses.py --batch NAME

Uses ONLY packet sizes and arrival times (the "size-auto" method validated in
Phase 6). For every run it writes, inside the run folder:
  pauses.csv     one row per stretch of speech or pause, in seconds from the
                 start of the clip:  kind,start_s,end_s,duration_s
  features.json  summary numbers for the clip (speech time, pause counts, ...)
With --batch it also writes /srv/iconect/batches/NAME/features.csv: one row
per clip, ready to open in Excel, R or Python.
"""
import argparse, csv, json, sys
from pathlib import Path

import dpkt
import numpy as np

PKT = 0.02            # one Opus packet = 20 ms
MERGE_GAP = 0.10      # gaps shorter than this inside speech are bridged
MIN_SEG = 0.06        # sound shorter than this is ignored
SIZE_MARGIN = 20      # bytes above silence that count as sound (marker search only)
LONG_PAUSE = 0.25     # pauses at least this long are counted separately
BATCH_ROOT = Path("/srv/iconect/batches")
VERSION = "extract_pauses 1.0 (size-auto, Otsu)"


def segments(times, active, frame):
    segs = []
    for t, a in zip(times, active):
        if not a:
            continue
        if segs and t - segs[-1][1] <= MERGE_GAP:
            segs[-1][1] = t + frame
        else:
            segs.append([t, t + frame])
    return [(s, e) for s, e in segs if e - s >= MIN_SEG]


def packets(run, t_play):
    """Sender's RTP audio packets: (seconds after play, UDP size)."""
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
                continue                                   # STUN / RTCP
            out.append((float(ts) - t_play, len(u)))
    return out


def find_marker(segs, onsets):
    g1, g2 = onsets[1] - onsets[0], onsets[2] - onsets[0]
    for i in range(len(segs) - 2):
        a, b, c = segs[i][0], segs[i + 1][0], segs[i + 2][0]
        if abs((b - a) - g1) < 0.08 and abs((c - a) - g2) < 0.08:
            return a - onsets[0]
    return None


def otsu(values):
    v = np.asarray(values, float)
    best, th = -1.0, float(np.median(v))
    for cut in np.unique(v)[:-1]:
        lo, hi = v[v <= cut], v[v > cut]
        score = len(lo) * len(hi) * (hi.mean() - lo.mean()) ** 2
        if score > best:
            best, th = score, cut + 0.5
    return th


def extract(run):
    meta = json.loads((run / "meta.json").read_text())
    if meta.get("status") != "ok":
        return {"status": f"skipped (run status {meta.get('status')})"}
    lay = meta["layout"]
    pk = packets(run, meta["t_play_ns"] / 1e9)
    if len(pk) < 100:
        return {"status": "too few audio packets"}
    times = np.array([p[0] for p in pk])
    sizes = np.array([p[1] for p in pk], float)
    settle = sizes[(times >= -8) & (times <= -1)]
    base = float(np.median(settle)) if settle.size else float(np.median(sizes))
    delay = find_marker(segments(times, sizes > base + SIZE_MARGIN, PKT), lay["marker_onsets_s"])
    if delay is None:
        return {"status": "sync marker not found in packets"}
    rel = times - delay
    c0, c1 = lay["clip_start_s"], lay["clip_end_s"]
    inclip = (rel >= c0) & (rel < c1)
    th = otsu(sizes[inclip])
    speech = [(s - c0, e - c0) for s, e in segments(rel[inclip], sizes[inclip] > th, PKT)]
    length = c1 - c0
    speech = [(max(0.0, s), min(length, e)) for s, e in speech if e > 0 and s < length]
    rows = []
    for i, (s, e) in enumerate(speech):
        if i:
            rows.append(("pause", speech[i - 1][1], s))
        rows.append(("speech", s, e))
    with open(run / "pauses.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["kind", "start_s", "end_s", "duration_s"])
        for k, s, e in rows:
            w.writerow([k, f"{s:.3f}", f"{e:.3f}", f"{e - s:.3f}"])
    pauses = np.array([e - s for k, s, e in rows if k == "pause"])
    long_p = pauses[pauses >= LONG_PAUSE]
    talk = sum(e - s for s, e in speech)
    span = (speech[-1][1] - speech[0][0]) if speech else 0.0
    feats = {
        "clip_id": meta.get("clip_id"), "run_id": meta.get("run_id"), "status": "ok",
        "method": VERSION,
        "settings": meta.get("settings"),
        "clip_length_s": round(length, 3),
        "delay_ms": round(delay * 1000, 1),
        "silence_bytes": round(base, 1), "threshold_bytes": th,
        "speech_segments": len(speech),
        "speech_time_s": round(talk, 3),
        "first_speech_s": round(speech[0][0], 3) if speech else None,
        "last_speech_end_s": round(speech[-1][1], 3) if speech else None,
        "speaking_span_s": round(span, 3),
        "speech_fraction_of_span": round(talk / span, 4) if span else None,
        "pauses_all": int(pauses.size),
        f"pauses_ge_{LONG_PAUSE}s": int(long_p.size),
        f"pauses_ge_{LONG_PAUSE}s_per_min": round(long_p.size / (span / 60), 2) if span else None,
        "pause_mean_s": round(float(long_p.mean()), 3) if long_p.size else None,
        "pause_median_s": round(float(np.median(long_p)), 3) if long_p.size else None,
        "pause_max_s": round(float(long_p.max()), 3) if long_p.size else None,
        "pause_total_s": round(float(long_p.sum()), 3) if long_p.size else 0.0,
    }
    (run / "features.json").write_text(json.dumps(feats, indent=2))
    return feats


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="*", help="run folders")
    ap.add_argument("--batch", help="use every successful run listed in this batch's status.csv")
    a = ap.parse_args()
    runs = [Path(r) for r in a.runs]
    bdir = None
    if a.batch:
        bdir = BATCH_ROOT / a.batch
        sp = bdir / "status.csv"
        if not sp.exists():
            sys.exit(f"No status table at {sp}")
        runs += [Path(r["run_dir"]) for r in csv.DictReader(open(sp)) if r["status"] == "ok" and r["run_dir"]]
    if not runs:
        sys.exit("Give run folders or --batch NAME")
    table = []
    for run in runs:
        try:
            f = extract(run)
        except Exception as e:                       # one bad run must not stop the rest
            f = {"status": f"error: {type(e).__name__}: {e}"}
        f.setdefault("clip_id", run.parent.name)
        f.setdefault("run_id", run.name)
        table.append(f)
        if f["status"] == "ok":
            print(f"{f['clip_id']}/{f['run_id']}: {f['speech_segments']} speech stretches, "
                  f"{f['speech_time_s']:.1f} s speech, {f[f'pauses_ge_{LONG_PAUSE}s']} pauses >= {LONG_PAUSE} s "
                  f"(median {f['pause_median_s']} s)")
        else:
            print(f"{f['clip_id']}/{f['run_id']}: {f['status']}")
    if bdir:
        cols = ["clip_id", "run_id", "status", "clip_length_s", "delay_ms", "silence_bytes", "threshold_bytes",
                "speech_segments", "speech_time_s", "first_speech_s", "last_speech_end_s", "speaking_span_s",
                "speech_fraction_of_span", "pauses_all", f"pauses_ge_{LONG_PAUSE}s",
                f"pauses_ge_{LONG_PAUSE}s_per_min", "pause_mean_s", "pause_median_s", "pause_max_s", "pause_total_s"]
        with open(bdir / "features.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for f in table:
                w.writerow(f)
        print(f"\nTable for the whole batch: {bdir / 'features.csv'}")


if __name__ == "__main__":
    main()
