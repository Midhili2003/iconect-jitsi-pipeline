"""patch7.py - received.wav check: if the chirps aren't near the expected spot,
search the whole recording, and report where they were expected.

Usage: python patch7.py /opt/iconect/validate.py
"""
import sys
p = sys.argv[1]
s = open(p).read()
edits = [
('''    lo_s, hi_s = 0.0, len(rx) / sr                  # where the first chirp may start
    if meta and meta.get("t_rec_start_ns") and meta.get("t_play_ns"):
        expect = (meta["t_play_ns"] - meta["t_rec_start_ns"]) / 1e9 + o[0]
        lo_s, hi_s = expect - 0.5, expect + 1.5     # newer runs: only near the expected spot
    cand = np.flatnonzero(score > 0.5)              # every strong chirp-like spot, earliest first
    starts, last = [], -1e9
    for k in cand:                                  # one candidate per 0.3 s
        if lo_s <= k / sr <= hi_s and k / sr - last > 0.3:
            starts.append(best(k / sr, k / sr + 0.25)[0]); last = k / sr
    found = None
    for t0 in starts:
        if t0 is None:
            continue
        hits = [(t0, float(score[int(t0 * sr)]))] + [best(t0 + g - 0.15, t0 + g + 0.15) for g in gaps]
        if all(h[0] is not None and h[1] > 0.4 for h in hits):
            found = hits
            break
    if found is None:
        return dict(rx_rate=rx_rate, marker_found=False)''',
'''    ranges, expect = [("whole file", 0.0, len(rx) / sr)], None
    if meta and meta.get("t_rec_start_ns") and meta.get("t_play_ns"):
        expect = (meta["t_play_ns"] - meta["t_rec_start_ns"]) / 1e9 + o[0]
        ranges.insert(0, ("near expected", expect - 0.5, expect + 1.5))   # try this first
    cand = np.flatnonzero(score > 0.5)              # every strong chirp-like spot, earliest first
    found, searched = None, None
    for name, lo_s, hi_s in ranges:
        starts, last = [], -1e9
        for k in cand:                              # one candidate per 0.3 s
            if lo_s <= k / sr <= hi_s and k / sr - last > 0.3:
                starts.append(best(k / sr, k / sr + 0.25)[0]); last = k / sr
        for t0 in starts:
            if t0 is None:
                continue
            hits = [(t0, float(score[int(t0 * sr)]))] + [best(t0 + g - 0.15, t0 + g + 0.15) for g in gaps]
            if all(h[0] is not None and h[1] > 0.4 for h in hits):
                found, searched = hits, name
                break
        if found:
            break
    exp_s = round(expect, 3) if expect is not None else None
    if found is None:
        top = sorted(((round(float(score[k]), 2), round(k / sr, 2)) for k in cand[::max(1, len(cand) // 200)]), reverse=True)[:5]
        return dict(rx_rate=rx_rate, marker_found=False, expected_at_s=exp_s, best_spots=top)'''),
('''    return dict(rx_rate=rx_rate, marker_found=True, marker_at_s=round(m0, 3),''',
 '''    return dict(rx_rate=rx_rate, marker_found=True, marker_at_s=round(m0, 3), expected_at_s=exp_s, searched=searched,'''),
('''        print(f"   received.wav ({rc['rx_rate']} Hz): marker NOT found")''',
 '''        print(f"   received.wav ({rc['rx_rate']} Hz): marker NOT found (expected at {rc['expected_at_s']} s; "
              f"most chirp-like spots (match, s): {rc['best_spots']})")'''),
('''        print(f"   received.wav ({rc['rx_rate']} Hz): marker at {rc['marker_at_s']} s (match {rc['match']}, "''',
 '''        print(f"   received.wav ({rc['rx_rate']} Hz): marker at {rc['marker_at_s']} s [expected {rc['expected_at_s']}, "
              f"searched {rc['searched']}] (match {rc['match']}, "'''),
]
for old, new in edits:
    n = s.count(old)
    if n != 1:
        sys.exit(f"PATCH FAILED: expected 1 match, found {n} for:\n{old[:80]}")
    s = s.replace(old, new)
open(p, "w").write(s)
print("patched OK")
