"""patch8.py - received.wav check: accept the marker when 2 of its 3 chirps are found.

Usage: python patch8.py /opt/iconect/validate.py
"""
import sys
p = sys.argv[1]
s = open(p).read()
NEW_FUNC = r'''
def received_check(run, lay, meta=None):
    """Find the 3 marker chirps in received.wav one at a time (robust to the
    receiver's jitter buffer stretching time, and to one chirp being lost)."""
    rp = run / "received.wav"
    if not rp.exists():
        return None
    rx, sr = sf.read(rp, dtype="float32")
    if rx.ndim > 1:
        rx = rx.mean(axis=1)
    st, sr2 = sf.read(run / "stimulus.wav", dtype="float32")
    rx_rate = sr
    if sr != sr2:                                   # resample to the stimulus rate
        n_new = int(len(rx) * sr2 / sr)
        rx = np.interp(np.arange(n_new) * sr / sr2, np.arange(len(rx)), rx).astype(np.float32)
        sr = sr2
    if len(rx) < sr:
        return None
    o, burst = lay["marker_onsets_s"], lay.get("marker_burst_s", 0.2)
    offs = [x - o[0] for x in o]                    # 0, 0.5, 1.3 s
    tpl = st[int(o[0] * sr):int((o[0] + burst) * sr)].astype(np.float64)    # one chirp
    n = len(tpl)
    nfft = 1 << int(np.ceil(np.log2(len(rx) + n)))
    xc = np.fft.irfft(np.fft.rfft(rx, nfft) * np.conj(np.fft.rfft(tpl, nfft)), nfft)[:len(rx)]
    c2 = np.concatenate([[0.0], np.cumsum(rx.astype(np.float64) ** 2)])
    idx = np.arange(len(rx))
    floor = n * 0.005 ** 2                          # quieter than -46 dB can't be a chirp
    energy = np.sqrt(np.maximum(c2[np.minimum(idx + n, len(rx))] - c2[idx], floor))
    score = xc / (energy * np.sqrt(np.sum(tpl ** 2)))   # 1.0 = perfect chirp shape

    def best(lo_s, hi_s):
        lo, hi = max(0, int(lo_s * sr)), min(len(rx), int(hi_s * sr))
        if hi <= lo:
            return None, 0.0
        k = lo + int(np.argmax(score[lo:hi]))
        return k / sr, float(score[k])

    ranges, expect = [("whole file", 0.0, len(rx) / sr)], None
    if meta and meta.get("t_rec_start_ns") and meta.get("t_play_ns"):
        expect = (meta["t_play_ns"] - meta["t_rec_start_ns"]) / 1e9 + o[0]
        ranges.insert(0, ("near expected", expect - 0.5, expect + offs[2] + 0.5))   # try this first
    cand = np.flatnonzero(score > 0.5)              # every strong chirp-like spot, earliest first
    found, searched = None, None
    for name, lo_s, hi_s in ranges:
        spots, last = [], -1e9
        for k in cand:                              # one spot per 0.3 s
            if lo_s <= k / sr <= hi_s and k / sr - last > 0.3:
                spots.append(best(k / sr, k / sr + 0.25)[0]); last = k / sr
        options = []
        for t in spots:                             # this spot could be chirp 1, 2 or 3
            for i in range(3):
                m0 = t - offs[i]
                hits = [best(m0 + d - 0.15, m0 + d + 0.15) for d in offs]
                good = [h if h[0] is not None and h[1] > 0.4 else (None, 0.0) for h in hits]
                count = sum(g[0] is not None for g in good)
                if count >= 2:
                    options.append((-count, m0, good))
        if options:
            options.sort(key=lambda x: (x[0], x[1]))   # most chirps found, then earliest
            found, searched = options[0][2], name
            break
    exp_s = round(expect, 3) if expect is not None else None
    if found is None:
        top = sorted(((round(float(score[k]), 2), round(k / sr, 2)) for k in cand[::max(1, len(cand) // 200)]), reverse=True)[:5]
        return dict(rx_rate=rx_rate, marker_found=False, expected_at_s=exp_s, best_spots=top)
    got = [(t, s_, d) for (t, s_), d in zip(found, offs) if t is not None]
    m0 = float(np.median([t - d for t, _, d in got]))      # first-chirp time implied by the chirps found
    peak = lambda a: float(np.max(np.abs(a))) if a.size else 0.0
    chirp_peaks = [peak(rx[int(t * sr):int((t + burst) * sr)]) for t, _, _ in got]
    before = rx[:max(0, int((m0 - 0.5) * sr))]
    c0 = int((m0 - o[0] + lay["clip_start_s"]) * sr)
    c1 = int((m0 - o[0] + lay["clip_end_s"]) * sr)
    return dict(rx_rate=rx_rate, marker_found=True, marker_at_s=round(m0, 3), expected_at_s=exp_s, searched=searched,
                chirps_found=len(got), match=round(float(np.mean([s_ for _, s_, _ in got])), 2),
                chirp_spacing_ms=[None if found[i][0] is None else round((found[i][0] - found[0][0] - offs[i]) * 1000)
                                  if found[0][0] is not None else None for i in (1, 2)],
                chirp_gain=round(float(np.median(chirp_peaks)) / 0.5, 2),
                peak_before_marker=round(peak(before), 3),
                time_of_peak_before=round(float(np.argmax(np.abs(before))) / sr, 2) if before.size else None,
                clip_peak=round(peak(rx[c0:c1]), 3),
                stim_clip_peak=round(peak(st[int(lay['clip_start_s']*sr):int(lay['clip_end_s']*sr)]), 3))

'''
a, b = s.find("def received_check("), s.find("def otsu(")
if s.count("def received_check(") != 1 or s.count("def otsu(") != 1 or not 0 < a < b:
    sys.exit("PATCH FAILED: could not find received_check() followed by otsu()")
s = s[:a] + NEW_FUNC.lstrip("\n") + s[b:]
old = "searched {rc['searched']}] (match {rc['match']}, \""
new = "searched {rc['searched']}] (chirps found {rc['chirps_found']}/3, match {rc['match']}, \""
if s.count(old) != 1:
    sys.exit("PATCH FAILED: print line not found exactly once")
s = s.replace(old, new)
open(p, "w").write(s)
print("patched OK")
