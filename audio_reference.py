#!/usr/bin/env python3
"""audio_reference.py - find the pauses in the ORIGINAL (unencrypted) recording,
as an automatic answer key when there are no hand marks.

Usage:
  /opt/mci-venv/bin/python /opt/iconect/audio_reference.py CLIP [CLIP ...]

For each audio file it writes CLIP_labels.txt next to it, in the same format
Audacity uses for exported labels, so score_real.py can compare it with what
the encrypted packets showed. It will NOT overwrite an existing labels file
(hand marks always win); use --force to replace it.

Method: loudness of each 20 ms slice in dB, then a speech/quiet line chosen
per recording with Otsu's method, then the same joining rules as the packet
method (gaps under 0.1 s bridged, blips under 0.06 s dropped).
"""
import argparse, subprocess, sys
from pathlib import Path

import numpy as np

SR = 48000
FR = 0.02
MERGE_GAP = 0.10
MIN_SEG = 0.06


def decode(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-ac", "1",
                          "-ar", str(SR), "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype="<f4")


def otsu(v):
    v = np.round(np.asarray(v, float), 1)
    best, th = -1.0, float(np.median(v))
    for cut in np.unique(v)[:-1]:
        lo, hi = v[v <= cut], v[v > cut]
        score = len(lo) * len(hi) * (hi.mean() - lo.mean()) ** 2
        if score > best:
            best, th = score, cut + 0.05
    return th


def speech_segments(x):
    n = int(len(x) / SR / FR)
    frames = x[:int(n * FR * SR)].reshape(n, -1)
    db = 20 * np.log10(np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1)) + 1e-9)
    db = np.maximum(db, -90)
    th = otsu(db)
    segs = []
    for i, active in enumerate(db > th):
        t = i * FR
        if not active:
            continue
        if segs and t - segs[-1][1] <= MERGE_GAP:
            segs[-1][1] = t + FR
        else:
            segs.append([t, t + FR])
    return [(s, e) for s, e in segs if e - s >= MIN_SEG], th, float(np.percentile(db, 10))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clips", nargs="+")
    ap.add_argument("--force", action="store_true", help="replace an existing labels file")
    a = ap.parse_args()
    for c in a.clips:
        clip = Path(c)
        out = clip.with_name(clip.stem + "_labels.txt")
        if out.exists() and not a.force:
            print(f"{clip.name}: {out.name} already exists - kept (use --force to replace)")
            continue
        try:
            segs, th, floor = speech_segments(decode(clip))
        except subprocess.CalledProcessError:
            print(f"{clip.name}: could not decode")
            continue
        pauses = [(segs[i][1], segs[i + 1][0]) for i in range(len(segs) - 1)]
        with open(out, "w") as f:
            for s, e in pauses:
                f.write(f"{s:.6f}\t{e:.6f}\tp\n")
        longp = [e - s for s, e in pauses if e - s >= 0.25]
        print(f"{clip.name}: {len(segs)} speech stretches, {len(pauses)} pauses "
              f"({len(longp)} >= 0.25 s); background {floor:.0f} dB, speech line {th:.0f} dB -> {out.name}")


if __name__ == "__main__":
    main()
