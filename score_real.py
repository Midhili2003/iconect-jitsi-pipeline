#!/usr/bin/env python3
"""score_real.py - compare pauses found in the encrypted packets with pauses
marked by hand in Audacity (or made by audio_reference.py).

Usage:
  /opt/mci-venv/bin/python /opt/iconect/score_real.py --batch NAME
      For every successful clip in the batch, looks for the marks next to
      the audio file: CLIP_labels.txt.
  /opt/mci-venv/bin/python /opt/iconect/score_real.py RUN_FOLDER LABELS.txt

Needs extract_pauses.py to have been run first (it reads each run's pauses.csv).
A marked pause counts as FOUND if a detected pause overlaps it.
A FALSE pause is a detected pause >= 0.25 s where nothing was marked.
"""
import argparse, csv, json, sys
from pathlib import Path

import numpy as np

BATCH_ROOT = Path("/srv/iconect/batches")
LONG = 0.25
BUCKETS = [(0.0, 0.25, "under 0.25 s"), (0.25, 0.5, "0.25-0.5 s"), (0.5, 1.0, "0.5-1 s"),
           (1.0, 2.0, "1-2 s"), (2.0, 1e9, "2 s and longer")]


def read_labels(path):
    """Audacity label export: start<TAB>end<TAB>text per line (lines starting with \\ are skipped)."""
    marks = []
    for line in open(path, encoding="utf-8-sig", errors="ignore"):
        parts = line.strip().split("\t")
        if len(parts) < 2 or line.startswith("\\"):
            continue
        try:
            s, e = float(parts[0].replace(",", ".")), float(parts[1].replace(",", "."))
        except ValueError:
            continue
        if e - s > 0.01:
            marks.append((s, e))
    return sorted(marks)


def read_detected(run):
    p = run / "pauses.csv"
    if not p.exists():
        sys.exit(f"{p} not found - run extract_pauses.py first")
    return [(float(r["start_s"]), float(r["end_s"])) for r in csv.DictReader(open(p)) if r["kind"] == "pause"]


def score(marks, det):
    rows = []
    for s, e in marks:
        best = max(det, key=lambda d: min(d[1], e) - max(d[0], s), default=None)
        if best is None or min(best[1], e) - max(best[0], s) <= 0:
            rows.append(dict(L=e - s, found=False))
        else:
            rows.append(dict(L=e - s, found=True, err=(best[1] - best[0]) - (e - s),
                             start=best[0] - s, end=best[1] - e))
    false = [d for d in det if d[1] - d[0] >= LONG
             and not any(min(d[1], e) - max(d[0], s) > 0 for s, e in marks)]
    return rows, false


def report(name, rows, false):
    ok = [r for r in rows if r["found"]]
    e = [abs(r["err"]) * 1000 for r in ok]
    print(f"{name}: marked {len(rows)} pauses, found {len(ok)}"
          + (f", median |length error| {np.median(e):.0f} ms, worst {max(e):.0f} ms" if e else "")
          + f" | false pauses >= {LONG} s: {len(false)}")


def bucket_table(rows):
    print(f"\n{'pause length':16} {'found':>9}  {'median error':>13}  {'worst':>8}  {'start':>7}  {'end':>7}")
    for lo, hi, label in BUCKETS:
        rs = [r for r in rows if lo <= r["L"] < hi]
        if not rs:
            continue
        ok = [r for r in rs if r["found"]]
        if ok:
            err = [r["err"] * 1000 for r in ok]
            print(f"{label:16} {len(ok):>4}/{len(rs):<4}  {np.median(err):>+10.0f} ms  {max(err, key=abs):>+5.0f} ms"
                  f"  {np.median([r['start'] for r in ok]) * 1000:>+4.0f} ms  {np.median([r['end'] for r in ok]) * 1000:>+4.0f} ms")
        else:
            print(f"{label:16} {0:>4}/{len(rs):<4}  {'-':>13}")
    print("(+ = detected pause longer than marked; start/end + = detected later than marked)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pair", nargs="*", help="RUN_FOLDER LABELS.txt")
    ap.add_argument("--batch", help="score every ok clip in this batch")
    a = ap.parse_args()
    jobs = []
    if a.batch:
        sp = BATCH_ROOT / a.batch / "status.csv"
        if not sp.exists():
            sys.exit(f"No status table at {sp}")
        for r in csv.DictReader(open(sp)):
            if r["status"] != "ok":
                continue
            clip = Path(r["clip_path"])
            lab = clip.with_name(clip.stem + "_labels.txt")
            if lab.exists():
                jobs.append((r["clip_id"], Path(r["run_dir"]), lab))
            else:
                print(f"{r['clip_id']}: no marks found ({lab}) - skipped")
    elif len(a.pair) == 2:
        jobs.append((Path(a.pair[0]).parent.name, Path(a.pair[0]), Path(a.pair[1])))
    else:
        sys.exit("Give --batch NAME, or RUN_FOLDER LABELS.txt")
    all_rows, all_false = [], []
    for name, run, lab in jobs:
        marks = read_labels(lab)
        if not marks:
            print(f"{name}: no marks could be read from {lab}")
            continue
        rows, false = score(marks, read_detected(run))
        report(name, rows, false)
        all_rows += rows
        all_false += false
    if all_rows:
        print("\n=== ALL CLIPS ===")
        report("total", all_rows, all_false)
        bucket_table(all_rows)


if __name__ == "__main__":
    main()
