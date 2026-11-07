"""patch5.py - received.wav check: ignore near-silent stretches, never crash.

Usage: python patch5.py /opt/iconect/validate.py
"""
import sys
p = sys.argv[1]
s = open(p).read()
edits = [
('''    energy = np.sqrt(np.maximum(c2[np.minimum(np.arange(len(rx)) + n, len(rx))] - c2[np.arange(len(rx))], 1e-12))''',
 '''    floor = n * 0.005 ** 2                      # stretches quieter than -46 dB can't be the marker
    energy = np.sqrt(np.maximum(c2[np.minimum(np.arange(len(rx)) + n, len(rx))] - c2[np.arange(len(rx))], floor))'''),
('''    chirp_peaks = [float(np.max(np.abs(rx[int((m0 + d) * sr):int((m0 + d + 0.2) * sr)])))
                   for d in (0.0, o[1] - o[0], o[2] - o[0])]''',
 '''    peak = lambda a: float(np.max(np.abs(a))) if a.size else 0.0
    chirp_peaks = [peak(rx[int((m0 + d) * sr):int((m0 + d + 0.2) * sr)])
                   for d in (0.0, o[1] - o[0], o[2] - o[0])]'''),
]
for old, new in edits:
    n = s.count(old)
    if n != 1:
        sys.exit(f"PATCH FAILED: expected 1 match, found {n} for:\n{old[:80]}")
    s = s.replace(old, new)
open(p, "w").write(s)
print("patched OK")
