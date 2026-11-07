#!/usr/bin/env python3
"""inspect_run.py - look inside one run: audio settings and packet-by-packet details.

Usage:  /opt/mci-venv/bin/python /opt/iconect/inspect_run.py RUN_FOLDER
"""
import collections, json, re, statistics, sys
from pathlib import Path

import dpkt
import numpy as np
import soundfile as sf

run = Path(sys.argv[1])
meta = json.loads((run / "meta.json").read_text())
lay = meta["layout"]

# ------------------------------------------------ 1. negotiated audio settings (SDP)
print("=== 1. Audio settings agreed by the sender (SDP) ===")
pcs = json.loads((run / "sender_webrtc.json").read_text())
pc = max(pcs, key=lambda p: len(p.get("stats", [])))          # the main connection
sdp = pc.get("localSdp") or ""
audio = sdp.split("m=audio", 1)[1].split("\nm=", 1)[0] if "m=audio" in sdp else ""
pt_names, ext_ids = {}, {}
for line in audio.splitlines():
    line = line.strip()
    if m := re.match(r"a=rtpmap:(\d+) (\S+)", line):
        pt_names[int(m[1])] = m[2]
    if m := re.match(r"a=extmap:(\d+)\S* (\S+)", line):
        ext_ids[int(m[1])] = m[2]
    if line.startswith(("a=rtpmap", "a=fmtp", "a=extmap")):
        print("  ", line)
level_id = next((i for i, u in ext_ids.items() if "ssrc-audio-level" in u), None)

out = [s for s in pc.get("stats", []) if s.get("type") == "outbound-rtp" and s.get("kind") == "audio"]
for s in out:
    print(f"   outbound audio: packetsSent={s.get('packetsSent')} bytesSent={s.get('bytesSent')} "
          f"headerBytesSent={s.get('headerBytesSent')} codecId={s.get('codecId')}")

# ------------------------------------------------ 2. ground truth from the stimulus
FR = 0.02                                                    # 20 ms = one Opus frame
x, sr = sf.read(run / "stimulus.wav", dtype="float32")
n = int(len(x) / sr / FR)
rms = np.array([np.sqrt(np.mean(x[int(i*FR*sr):int((i+1)*FR*sr)] ** 2)) for i in range(n)])
sound = rms > 0.01                                           # sound = louder than -40 dBFS

def label(t_rel):
    """t_rel = seconds after playback started -> 'sound' / 'silence' / 'outside'."""
    i = int(t_rel / FR)
    if t_rel < 0 or i >= n:
        return "outside"
    return "sound" if sound[i] else "silence"

# ------------------------------------------------ 3. every packet to the Videobridge
t_play = meta["t_play_ns"] / 1e9
kinds = collections.Counter()
by_label = collections.defaultdict(list)                     # label -> [udp sizes]
lvl_by_label = collections.defaultdict(list)                 # label -> [audio levels]
pt_count, ssrc_count = collections.Counter(), collections.Counter()
with open(run / "uplink.pcap", "rb") as f:
    r = dpkt.pcap.Reader(f)
    for ts, buf in r:
        try:
            eth = dpkt.ethernet.Ethernet(buf)
        except Exception:
            continue
        ip = eth.data
        if not isinstance(ip, (dpkt.ip.IP, dpkt.ip6.IP6)) or not isinstance(ip.data, dpkt.udp.UDP):
            continue
        u = ip.data
        if u.dport != 10000:
            continue
        p = bytes(u.data)
        if len(p) < 12 or p[0] >> 6 != 2:                    # not RTP/RTCP (e.g. STUN checks)
            kinds["stun/other"] += 1
            continue
        if 192 <= p[1] <= 223:                               # RTCP shares the port
            kinds["rtcp"] += 1
            continue
        kinds["rtp"] += 1
        pt = p[1] & 0x7F
        ssrc = int.from_bytes(p[8:12], "big")
        pt_count[pt] += 1
        ssrc_count[ssrc] += 1
        lab = label(float(ts) - t_play)
        by_label[lab].append(len(u))
        # audio level header extension (sent unencrypted in the RTP header)
        cc, pos = p[0] & 0x0F, 12 + 4 * (p[0] & 0x0F)
        if p[0] & 0x10 and level_id and len(p) >= pos + 4 and p[pos:pos+2] == b"\xbe\xde":
            words = int.from_bytes(p[pos+2:pos+4], "big")
            q, end = pos + 4, pos + 4 + 4 * words
            while q < end and q < len(p):
                if p[q] == 0:
                    q += 1; continue
                eid, elen = p[q] >> 4, (p[q] & 0x0F) + 1
                if eid == level_id and q + 1 < len(p):
                    lvl_by_label[lab].append(p[q+1] & 0x7F)       # 0 = loudest, 127 = silent
                q += 1 + elen

print("\n=== 2. Packets from the sender to the Videobridge ===")
print("   by kind:", dict(kinds))
print("   RTP by payload type:", {f"{k} ({pt_names.get(k, '?')})": v for k, v in pt_count.items()})
print("   RTP streams (SSRC):", len(ssrc_count), dict(ssrc_count))

print("\n=== 3. Packet sizes (UDP bytes) by what the stimulus was doing ===")
for lab in ("silence", "sound", "outside"):
    v = by_label.get(lab, [])
    if not v:
        continue
    q = np.percentile(v, [10, 50, 90])
    common = collections.Counter(v).most_common(6)
    print(f"   {lab:8} {len(v):5} packets   10%/50%/90% = {q[0]:.0f}/{q[1]:.0f}/{q[2]:.0f}   "
          f"most common sizes: {common}")

print("\n=== 4. Unencrypted audio-level values (0 = loudest, 127 = silence) ===")
if not level_id:
    print("   no audio-level extension negotiated")
for lab in ("silence", "sound", "outside"):
    v = lvl_by_label.get(lab, [])
    if v:
        q = np.percentile(v, [10, 50, 90])
        print(f"   {lab:8} {len(v):5} values      10%/50%/90% = {q[0]:.0f}/{q[1]:.0f}/{q[2]:.0f}")

print("\n=== 5. Silence packets over time (is the size level switching?) ===")
rows = []
with open(run / "uplink.pcap", "rb") as f:
    for ts, buf in dpkt.pcap.Reader(f):
        try:
            u = dpkt.ethernet.Ethernet(buf).data.data
        except Exception:
            continue
        if isinstance(u, dpkt.udp.UDP) and u.dport == 10000 and len(u.data) >= 12 \
                and bytes(u.data)[0] >> 6 == 2 and not 192 <= bytes(u.data)[1] <= 223:
            rows.append((float(ts) - t_play, len(u)))
for start in np.arange(-10, lay["total_s"] + 2, 1.0):
    v = [s for t, s in rows if start <= t < start + 1 and label(t) in ("silence", "outside")]
    if v:
        print(f"   {start:6.1f}s  silent packets: {len(v):3}  median size {statistics.median(v):5.0f}")
