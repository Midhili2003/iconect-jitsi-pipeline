"""patch4.py - find the marker in received.wav near where it should be.

Usage: python patch4.py /opt/iconect/run_clip.py /opt/iconect/validate.py
"""
import sys
rc_path, va_path = sys.argv[1], sys.argv[2]

def apply(path, edits):
    s = open(path).read()
    for old, new in edits:
        n = s.count(old)
        if n != 1:
            sys.exit(f"PATCH FAILED in {path}: expected 1 match, found {n} for:\n{old[:80]}")
        s = s.replace(old, new)
    return s

# 1. run_clip.py: remember when the receiver's recording started
rc = apply(rc_path, [(
'''                 str(rdir / "received.wav")], env=env)
''',
'''                 str(rdir / "received.wav")], env=env)
            meta["t_rec_start_ns"] = time.time_ns()     # receiver recording started
''')])

# 2. validate.py: search only near the expected spot, with a fair (normalised) match score
va = apply(va_path, [
('''def received_check(run, lay):''', '''def received_check(run, lay, meta=None):'''),
('''    nfft = 1 << int(np.ceil(np.log2(len(rx) + len(tpl))))
    xc = np.fft.irfft(np.fft.rfft(rx, nfft) * np.conj(np.fft.rfft(tpl, nfft)), nfft)[:len(rx)]
    k = int(np.argmax(xc))''',
'''    nfft = 1 << int(np.ceil(np.log2(len(rx) + len(tpl))))
    xc = np.fft.irfft(np.fft.rfft(rx, nfft) * np.conj(np.fft.rfft(tpl, nfft)), nfft)[:len(rx)]
    # normalise: divide by the loudness of each stretch, so a loud clip can't win by volume alone
    c2 = np.concatenate([[0.0], np.cumsum(rx.astype(np.float64) ** 2)])
    n = len(tpl)
    energy = np.sqrt(np.maximum(c2[np.minimum(np.arange(len(rx)) + n, len(rx))] - c2[np.arange(len(rx))], 1e-12))
    score = xc / (energy * np.sqrt(np.sum(tpl.astype(np.float64) ** 2)) + 1e-12)
    # search only near where the marker should be, if the run recorded when the recording started
    if meta and meta.get("t_rec_start_ns") and meta.get("t_play_ns"):
        expect = (meta["t_play_ns"] - meta["t_rec_start_ns"]) / 1e9 + (o[0] - 0.05)
        lo, hi = max(0, int((expect - 0.5) * sr)), min(len(rx), int((expect + 2.0) * sr))
        if hi > lo:
            mask = np.full(len(rx), -np.inf)
            mask[lo:hi] = 0.0
            score = score + mask
    k = int(np.argmax(score))'''),
('''    return dict(rx_rate=rx_rate, marker_at_s=round(m0, 3),''',
'''    return dict(rx_rate=rx_rate, marker_at_s=round(m0, 3), match=round(float(score[k]), 2),'''),
('''    rc = received_check(run, lay)''', '''    rc = received_check(run, lay, meta)'''),
('''        print(f"   received.wav ({rc['rx_rate']} Hz): marker at {rc['marker_at_s']} s, chirp gain x{rc['chirp_gain']}, "''',
'''        print(f"   received.wav ({rc['rx_rate']} Hz): marker at {rc['marker_at_s']} s (match {rc['match']}), chirp gain x{rc['chirp_gain']}, "'''),
])
open(rc_path, "w").write(rc)
open(va_path, "w").write(va)
print("patched OK")
