import sys
def patch(path, edits):
    s = open(path).read()
    for old, new in edits:
        n = s.count(old)
        if n != 1:
            sys.exit(f"PATCH FAILED in {path}: expected 1 match, found {n} for:\n{old[:80]}")
        s = s.replace(old, new)
    open(path, "w").write(s)
    print(f"patched OK: {path}")

# validate.py: accept received.wav at any sample rate / channel count
patch(sys.argv[1], [(
'''    if sr != sr2 or len(rx) < sr:
        return None''',
'''    rx_rate = sr
    if sr != sr2:                                   # resample to the stimulus rate
        n_new = int(len(rx) * sr2 / sr)
        rx = np.interp(np.arange(n_new) * sr / sr2, np.arange(len(rx)), rx).astype(np.float32)
        sr = sr2
    if len(rx) < sr:
        return None'''),
('''    return dict(marker_at_s=round(m0, 3),''',
'''    return dict(rx_rate=rx_rate, marker_at_s=round(m0, 3),'''),
('''        print(f"   received.wav: marker at''',
'''        print(f"   received.wav ({rc['rx_rate']} Hz): marker at''')])

# run_clip.py: record the receiver at 48 kHz mono, like everything else
patch(sys.argv[2], [(
'''["parecord", "--device=spk.monitor", "--latency-msec=20", "--file-format=wav",''',
'''["parecord", "--device=spk.monitor", "--latency-msec=20", "--rate=48000", "--channels=1",
                 "--file-format=wav",''')])
