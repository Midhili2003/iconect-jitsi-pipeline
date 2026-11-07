"""patch6.py - received.wav check: find the 3 marker chirps one at a time.

Usage: python patch6.py /opt/iconect/validate.py
"""
import sys
p = sys.argv[1]
s = open(p).read()
NEW_FUNC = r'''
def received_check(run, lay, meta=None):
    """Find the 3 marker chirps in received.wav one at a time (robust to the
    receiver's jitter buffer stretching or squeezing time a little)."""
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

    gaps = [o[1] - o[0], o[2] - o[0]]
    lo_s, hi_s = 0.0, len(rx) / sr                  # where the first chirp may start
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
        return dict(rx_rate=rx_rate, marker_found=False)
    m0 = found[0][0]
    peak = lambda a: float(np.max(np.abs(a))) if a.size else 0.0
    chirp_peaks = [peak(rx[int(t * sr):int((t + burst) * sr)]) for t, _ in found]
    before = rx[:max(0, int((m0 - 0.5) * sr))]
    c0 = int((m0 - o[0] + lay["clip_start_s"]) * sr)
    c1 = int((m0 - o[0] + lay["clip_end_s"]) * sr)
    return dict(rx_rate=rx_rate, marker_found=True, marker_at_s=round(m0, 3),
                match=round(float(np.mean([h[1] for h in found])), 2),
                chirp_spacing_ms=[round((found[i][0] - m0 - (o[i] - o[0])) * 1000) for i in (1, 2)],
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
old = '''    if rc:
        print(f"   received.wav ({rc['rx_rate']} Hz): marker at {rc['marker_at_s']} s (match {rc['match']}), chirp gain x{rc['chirp_gain']}, "'''
new = '''    if rc and not rc.get("marker_found", True):
        print(f"   received.wav ({rc['rx_rate']} Hz): marker NOT found")
    elif rc:
        print(f"   received.wav ({rc['rx_rate']} Hz): marker at {rc['marker_at_s']} s (match {rc['match']}, "
              f"chirps 2+3 off by {rc['chirp_spacing_ms']} ms), chirp gain x{rc['chirp_gain']}, "'''
if s.count(old) != 1:
    sys.exit("PATCH FAILED: print line not found exactly once")
s = s.replace(old, new)
open(p, "w").write(s)
print("patched OK")
