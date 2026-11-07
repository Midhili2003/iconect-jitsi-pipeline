#!/usr/bin/env python3
"""validate.py - how accurately do the packets reveal sound and pauses?

Usage:  /opt/mci-venv/bin/python /opt/iconect/validate.py RUN_FOLDER [RUN_FOLDER ...]

For each run it:
  1. finds sound in the packets alone (two methods: packet SIZE, and the
     unencrypted audio LEVEL header value),
  2. finds the 3-chirp sync marker to measure the real delay,
  3. compares with the truth in stimulus.wav (onset/offset/pause errors),
  4. checks received.wav (marker found? how loud? anything before it?),
  5. if the clip has a truth file next to it (CLIP.truth.json, made by
     make_test_clips.py), checks every designed pause one by one and
     reports the error for each pause length.
The "-auto" methods learn the quiet level from the clip itself (Otsu's
method: the cut that best splits the packets into two groups), so they
keep working when the pauses are full of background noise.
"""
import json, re, sys
from pathlib import Path

import dpkt
import numpy as np
import soundfile as sf

PKT = 0.02            # one Opus packet = 20 ms
MERGE_GAP = 0.10      # gaps shorter than this inside sound are bridged
MIN_SEG = 0.06        # sound shorter than this is ignored
SIZE_MARGIN = 20      # bytes above the silent baseline that count as sound
LEVEL_SOUND = 120     # audio-level value below this counts as sound (127 = silent)
REPORT_PAUSE = 0.25   # false pauses at least this long are counted as errors

# ------------------------------------------------------------ helpers
def segments(times, active, frame):
    """Active flags at given times -> merged (start, end) segments."""
    segs = []
    for t, a in zip(times, active):
        if not a:
            continue
        if segs and t - segs[-1][1] <= MERGE_GAP:
            segs[-1][1] = t + frame
        else:
            segs.append([t, t + frame])
    return [(s, e) for s, e in segs if e - s >= MIN_SEG]

def truth(run):
    x, sr = sf.read(run / "stimulus.wav", dtype="float32")
    fr = 0.01
    n = int(len(x) / sr / fr)
    rms = np.array([np.sqrt(np.mean(x[int(i*fr*sr):int((i+1)*fr*sr)] ** 2)) for i in range(n)])
    return segments(np.arange(n) * fr, rms > 0.01, fr)

def level_ext_id(run):
    try:
        pcs = json.loads((run / "sender_webrtc.json").read_text())
        sdp = max(pcs, key=lambda p: len(p.get("stats", []))).get("localSdp") or ""
        m = re.search(r"a=extmap:(\d+)\S* urn:ietf:params:rtp-hdrext:ssrc-audio-level", sdp)
        return int(m[1]) if m else None
    except Exception:
        return None

def packets(run, t_play, lid):
    """Sender's RTP audio packets: (seconds after play, UDP size, audio level or None)."""
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
            lvl, pos = None, 12 + 4 * (p[0] & 0x0F)
            if lid and p[0] & 0x10 and p[pos:pos+2] == b"\xbe\xde":
                q, end = pos + 4, pos + 4 + 4 * int.from_bytes(p[pos+2:pos+4], "big")
                while q < min(end, len(p)):
                    if p[q] == 0:
                        q += 1; continue
                    eid, ln = p[q] >> 4, (p[q] & 0x0F) + 1
                    if eid == lid and q + 1 < len(p):
                        lvl = p[q+1] & 0x7F
                    q += 1 + ln
            out.append((float(ts) - t_play, len(u), lvl))
    return out

def find_marker(segs, onsets):
    """Three detected segments whose start gaps match the marker's -> delay (s)."""
    g1, g2 = onsets[1] - onsets[0], onsets[2] - onsets[0]
    for i in range(len(segs) - 2):
        a, b, c = segs[i][0], segs[i+1][0], segs[i+2][0]
        if abs((b - a) - g1) < 0.08 and abs((c - a) - g2) < 0.08:
            return a - onsets[0]
    return None

def compare(det, tru, clip_start, clip_end):
    """Match each true clip segment to the detected one it overlaps most."""
    tru = [s for s in tru if s[0] >= clip_start - 0.01 and s[1] <= clip_end + 0.01]
    det_in = [s for s in det if s[1] > clip_start - 0.5 and s[0] < clip_end + 0.5]
    on, off, matched = [], [], []
    for ts, te in tru:
        best = max(det_in, key=lambda d: min(d[1], te) - max(d[0], ts), default=None)
        if best is None or min(best[1], te) - max(best[0], ts) <= 0:
            matched.append(None); continue
        matched.append(best)
        on.append(best[0] - ts); off.append(best[1] - te)
    pauses = []
    for k in range(len(tru) - 1):
        a, b = matched[k], matched[k+1]
        if a and b and a is not b:
            pauses.append((b[0] - a[1]) - (tru[k+1][0] - tru[k][1]))
    used = {id(m) for m in matched if m}
    extra = sum(1 for d in det_in if id(d) not in used)
    missed = sum(1 for m in matched if m is None)
    # frame-by-frame agreement in the clip region, 10 ms steps
    grid = np.arange(clip_start, clip_end, 0.01)
    inside = lambda segs: np.array([any(s <= t < e for s, e in segs) for t in grid])
    agree = float(np.mean(inside(tru) == inside(det_in))) if len(grid) else float("nan")
    return dict(n=len(tru), missed=missed, extra=extra, on=on, off=off, pause=pauses, agree=agree)

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

def otsu(values):
    """Threshold that best splits values into a low group and a high group."""
    v = np.asarray(values, float)
    best, th = -1.0, float(np.median(v))
    for cut in np.unique(v)[:-1]:
        lo, hi = v[v <= cut], v[v > cut]
        score = len(lo) * len(hi) * (hi.mean() - lo.mean()) ** 2
        if score > best:
            best, th = score, cut + 0.5
    return th

def designed_pauses(meta, lay):
    """Designed pauses from CLIP.truth.json, in stimulus seconds, or None."""
    tp = Path(meta.get("clip_path", "")).with_suffix(".truth.json")
    if not tp.is_file():
        return None
    t = json.loads(tp.read_text())
    c0 = lay["clip_start_s"]
    return [(c0 + p["start_s"], c0 + p["start_s"] + p["length_s"], p["length_s"]) for p in t["pauses"]]

def false_pauses(det, pauses, lay):
    """Detected gaps inside the clip that match no designed pause, >= REPORT_PAUSE long."""
    c0, c1 = lay["clip_start_s"] + 0.5, lay["clip_end_s"] - 0.5
    gaps = [(det[i][1], det[i+1][0]) for i in range(len(det) - 1) if det[i][1] > c0 and det[i+1][0] < c1]
    return [g for g in gaps if g[1] - g[0] >= REPORT_PAUSE
            and not any(min(g[1], pe) - max(g[0], ps) > 0 for ps, pe, _ in pauses)]

def pause_check(det, pauses):
    """For each designed pause, find the detected silent gap that overlaps it most."""
    gaps = [(det[i][1], det[i+1][0]) for i in range(len(det) - 1)]
    rows = []
    for ps, pe, L in pauses:
        best = max(gaps, key=lambda g: min(g[1], pe) - max(g[0], ps), default=None)
        if best is None or min(best[1], pe) - max(best[0], ps) <= 0:
            rows.append(dict(L=L, found=False))            # pause hidden (merged into speech)
        else:
            rows.append(dict(L=L, found=True, err=(best[1] - best[0]) - L,
                             start=best[0] - ps, end=best[1] - pe))
    return rows

def pause_table(rows, indent="      "):
    for L in sorted({r["L"] for r in rows}):
        rs = [r for r in rows if r["L"] == L]
        ok = [r for r in rs if r["found"]]
        e = [r["err"] * 1000 for r in ok]
        txt = (f"length error median {np.median(e):+5.0f} ms, worst {max(e, key=abs):+5.0f} ms | "
               f"start {np.median([r['start'] for r in ok])*1000:+4.0f} ms, end {np.median([r['end'] for r in ok])*1000:+4.0f} ms") if ok else "-"
        print(f"{indent}{L:4.2f} s pauses: found {len(ok)}/{len(rs)} | {txt}")

ms = lambda v: f"{np.median(np.abs(v))*1000:5.0f}" if v else "    -"

# ------------------------------------------------------------ main
METHODS = ("size", "size-auto", "level", "level-auto")
summary = {m: [] for m in METHODS}
pause_rows = {m: [] for m in METHODS}
false_count = {m: 0 for m in METHODS}
for arg in sys.argv[1:]:
    run = Path(arg)
    meta = json.loads((run / "meta.json").read_text())
    if meta.get("status") != "ok":
        print(f"\n{run.name}: skipped (status {meta.get('status')})")
        continue
    lay, t_play = meta["layout"], meta["t_play_ns"] / 1e9
    pk = packets(run, t_play, level_ext_id(run))
    tru = truth(run)
    dp = designed_pauses(meta, lay)
    times = [p[0] for p in pk]
    settle = [p[1] for p in pk if -8 <= p[0] <= -1]
    base = float(np.median(settle)) if settle else float(np.median([p[1] for p in pk]))
    print(f"\n=== {run.parent.name}/{run.name} ===   silent baseline {base:.0f} bytes, "
          f"{len(pk)} audio packets")
    delays = {}
    for method in METHODS:
        kind = method.split("-")[0]
        if kind == "level" and all(p[2] is None for p in pk):
            continue
        # value that grows with loudness: packet size, or minus the level (0 loud, 127 silent)
        val = np.array([p[1] if kind == "size" else -(p[2] if p[2] is not None else 127) for p in pk], float)
        if not method.endswith("auto"):
            act = val > (base + SIZE_MARGIN if kind == "size" else -LEVEL_SOUND)
            det = segments(times, act, PKT)
            delay = find_marker(det, lay["marker_onsets_s"])
            if delay is None:
                print(f"   {method:10}: marker NOT found ({len(det)} sound segments detected)")
                continue
            delays[kind] = delay
        else:
            if kind not in delays:          # the marker is found by the fixed method
                continue
            delay = delays[kind]
            rel = np.array(times) - delay
            inclip = (rel >= lay["clip_start_s"]) & (rel < lay["clip_end_s"])
            th = otsu(val[inclip])
            det = segments(times, val > th, PKT)
        det = [(s - delay, e - delay) for s, e in det]
        r = compare(det, tru, lay["clip_start_s"], lay["clip_end_s"])
        r["delay"] = delay
        summary[method].append(r)
        print(f"   {method:10}: delay {delay*1000:5.0f} ms | segments {r['n']} (missed {r['missed']}, extra {r['extra']}) | "
              f"median error: start {ms(r['on'])} ms, end {ms(r['off'])} ms, pause {ms(r['pause'])} ms | "
              f"agreement {r['agree']*100:5.1f}%")
        if dp:
            rows = pause_check(det, dp)
            pause_rows[method] += rows
            fp = false_pauses(det, dp, lay)
            false_count[method] += len(fp)
            e = [abs(x["err"]) for x in rows if x["found"]]
            print(f"               designed pauses: found {sum(x['found'] for x in rows)}/{len(rows)}, "
                  + (f"median |length error| {np.median(e)*1000:.0f} ms, worst {max(e)*1000:.0f} ms" if e else "-")
                  + f" | false pauses >= {REPORT_PAUSE} s: {len(fp)}")
    rc = received_check(run, lay, meta)
    if rc and not rc.get("marker_found", True):
        print(f"   received.wav ({rc['rx_rate']} Hz): marker NOT found (expected at {rc['expected_at_s']} s; "
              f"most chirp-like spots (match, s): {rc['best_spots']})")
    elif rc:
        print(f"   received.wav ({rc['rx_rate']} Hz): marker at {rc['marker_at_s']} s [expected {rc['expected_at_s']}, "
              f"searched {rc['searched']}] (chirps found {rc['chirps_found']}/3, match {rc['match']}, "
              f"chirps 2+3 off by {rc['chirp_spacing_ms']} ms), chirp gain x{rc['chirp_gain']}, "
              f"clip peak {rc['clip_peak']} (played {rc['stim_clip_peak']}), "
              f"loudest thing before marker {rc['peak_before_marker']} at {rc['time_of_peak_before']} s")

print("\n=== SUMMARY over all runs ===")
for method, rs in summary.items():
    if not rs:
        continue
    cat = lambda k: [v for r in rs for v in r[k]]
    d = [r["delay"] * 1000 for r in rs]
    print(f"   {method:10}: {len(rs)} runs | delay {np.median(d):.0f} ms (range {min(d):.0f}-{max(d):.0f}) | "
          f"missed {sum(r['missed'] for r in rs)}, extra {sum(r['extra'] for r in rs)} | "
          f"median |error|: start {ms(cat('on'))} ms, end {ms(cat('off'))} ms, pause {ms(cat('pause'))} ms | "
          f"worst pause error {max(map(abs, cat('pause')), default=0)*1000:.0f} ms | "
          f"agreement {np.mean([r['agree'] for r in rs])*100:.1f}%")

for method, rows in pause_rows.items():
    if rows:
        print(f"\n=== DESIGNED PAUSES by length, {method} method (all runs; + = detected pause too long; "
              f"false pauses >= {REPORT_PAUSE} s: {false_count[method]}) ===")
        pause_table(rows)
