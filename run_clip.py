#!/usr/bin/env python3
"""run_clip.py - turn one audio clip into one encrypted Jitsi network trace.

Usage:
    /opt/mci-venv/bin/python /opt/iconect/run_clip.py CLIP.wav [--clip-id ID]

Output: /srv/iconect/runs/<clip_id>/<run_id>/ containing
    stimulus.wav         exactly what was played into the sender's fake mic
    uplink.pcap          everything on the sender's cable (veth-mci0)
    received.wav         what the receiver heard (sanity check)
    sender_webrtc.json   sender's SDP + WebRTC statistics
    meta.json            timing, layout, settings, versions, checks, status
    logs/                tcpdump, PulseAudio, Xvfb, browser console logs
"""
import argparse, hashlib, json, os, random, shutil, signal, string
import subprocess, sys, time, traceback
from pathlib import Path

import numpy as np
import soundfile as sf

# ----------------------------------------------------------------- settings
JITSI = "https://iconect-jitsi.hdanny.org/"
RUNS_ROOT = Path("/srv/iconect/runs")
WRAPPER = "/usr/local/bin/chrome-mcisend"   # starts Chromium inside mcisend
CABLE = "veth-mci0"                          # sender's cable, server side
LOCK = Path("/run/mci-run.lock")             # only one call at a time
SR = 48000                                   # Jitsi audio runs at 48 kHz

# stimulus layout (seconds): lead | 3-chirp marker | gap | clip | tail
LEAD_S, GAP_S, TAIL_S = 1.0, 1.5, 1.0
BURST_S, BURST_GAPS = 0.20, (0.30, 0.60)    # uneven gaps: can't be confused

# ----------------------------------------------------------------- helpers
def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()

def out(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception as e:
        return f"<error: {e}>"

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def decode(path, channel):
    """Any audio file -> float32 mono 48 kHz (optionally one channel only)."""
    af = ["-af", f"pan=mono|c0=c{channel}"] if channel is not None else ["-ac", "1"]
    raw = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), *af,
                          "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype="<f4").copy()

def chirp(dur, f0=300.0, f1=3000.0, amp=0.5):
    t = np.arange(int(dur * SR)) / SR
    x = amp * np.sin(2 * np.pi * (f0 * t + 0.5 * (f1 - f0) / dur * t ** 2))
    ramp = int(0.01 * SR)                     # 10 ms fade in/out, no clicks
    w = np.ones_like(x)
    w[:ramp] = np.linspace(0, 1, ramp)
    w[-ramp:] = np.linspace(1, 0, ramp)
    return (x * w).astype(np.float32)

def build_stimulus(clip):
    z = lambda s: np.zeros(int(round(s * SR)), np.float32)
    parts, onsets, cur = [z(LEAD_S)], [], LEAD_S
    for i in range(3):
        onsets.append(round(cur, 6))
        parts.append(chirp(BURST_S)); cur += BURST_S
        if i < 2:
            parts.append(z(BURST_GAPS[i])); cur += BURST_GAPS[i]
    parts.append(z(GAP_S)); cur += GAP_S
    clip_start = cur
    parts.append(clip.astype(np.float32)); cur += len(clip) / SR
    parts.append(z(TAIL_S))
    sig = np.concatenate(parts)
    peak = float(np.max(np.abs(sig)))
    if peak > 1.0:                            # never clip: scale down instead
        sig = sig / peak
    return sig, {"sample_rate": SR, "marker_onsets_s": onsets, "marker_burst_s": BURST_S,
                 "clip_start_s": round(clip_start, 6), "clip_end_s": round(cur, 6),
                 "total_s": round(len(sig) / SR, 6),
                 "rescaled_from_peak": peak if peak > 1.0 else None}

def wait_for_file_text(path, text, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if path.exists() and text in path.read_text(errors="ignore"):
            return True
        time.sleep(0.1)
    return False

def stop(proc, sig=signal.SIGINT, timeout=10):
    if proc and proc.poll() is None:
        proc.send_signal(sig)
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait()

# ----------------------------------------------------------------- browser JS
RECORD_PCS = """(() => {
  const Orig = window.RTCPeerConnection;
  if (!Orig || Orig.__mci) return;
  window.__mciPCs = [];
  function W(...a) { const pc = new Orig(...a); window.__mciPCs.push(pc); return pc; }
  W.prototype = Orig.prototype; Object.setPrototypeOf(W, Orig); W.__mci = true;
  window.RTCPeerConnection = W;
})();"""
JOINED = "() => window.APP && APP.conference && APP.conference.isJoined()"
MEMBERS = "() => { try { return APP.conference.membersCount; } catch (e) { return 0; } }"
MIC_LABEL = """() => { const t = APP.store.getState()['features/base/tracks']
    .find(t => t.local && t.mediaType === 'audio');
  return t ? t.jitsiTrack.getTrack().label : null; }"""
EFFECTIVE_CFG = """() => { const c = APP.store.getState()['features/base/config'];
  return { p2p_enabled: c.p2p && c.p2p.enabled, audioQuality: c.audioQuality || null,
           disableAP: c.disableAP, disableAEC: c.disableAEC, disableNS: c.disableNS,
           disableAGC: c.disableAGC, prejoin_enabled: c.prejoinConfig && c.prejoinConfig.enabled,
           jitsi_lib_version: (window.JitsiMeetJS && JitsiMeetJS.version) || null }; }"""
DUMP_PCS = """async () => {
  const res = [];
  for (const pc of (window.__mciPCs || [])) {
    const e = { state: pc.connectionState,
                localSdp: pc.localDescription && pc.localDescription.sdp,
                remoteSdp: pc.remoteDescription && pc.remoteDescription.sdp, stats: [] };
    try { (await pc.getStats()).forEach(s => e.stats.push(s)); } catch (x) { e.err = String(x); }
    res.push(e);
  }
  return res; }"""

def jitsi_url(room, name, sender, dtx, audio_processing):
    s = {"config.p2p.enabled": "false", "config.prejoinConfig.enabled": "false",
         "config.enableNoAudioDetection": "false", "config.startWithVideoMuted": "true",
         "config.startAudioOnly": "true", "config.disableDeepLinking": "true",
         "config.startWithAudioMuted": "false" if sender else "true",
         "userInfo.displayName": f"%22{name}%22"}
    if sender:
        s["config.audioQuality.enableOpusDtx"] = "true" if dtx else "false"
        if not audio_processing:
            for k in ("disableAP", "disableAEC", "disableNS", "disableAGC", "disableHPF"):
                s[f"config.{k}"] = "true"
    return JITSI + room + "#" + "&".join(f"{k}={v}" for k, v in s.items())

# ----------------------------------------------------------------- main run
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clip", help="audio file (wav, mp3, ...)")
    ap.add_argument("--clip-id", help="name for this clip (default: file name)")
    ap.add_argument("--channel", type=int, help="use only this input channel (0 = first)")
    ap.add_argument("--settle", type=float, default=10.0, help="seconds to wait after unmuting")
    ap.add_argument("--dtx", action="store_true", help="turn ON Opus silence suppression (default off)")
    ap.add_argument("--no-audio-processing", action="store_true",
                    help="turn OFF browser echo/noise/gain processing (default on)")
    a = ap.parse_args()

    clip = Path(a.clip).resolve()
    clip_id = a.clip_id or clip.stem
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + "".join(
        random.choices(string.ascii_lowercase + string.digits, k=4))
    rdir = RUNS_ROOT / clip_id / run_id
    (rdir / "logs").mkdir(parents=True)
    meta = {"run_id": run_id, "clip_id": clip_id, "clip_path": str(clip),
            "status": "running", "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "settings": {"settle_s": a.settle, "opus_dtx": a.dtx,
                         "browser_audio_processing": not a.no_audio_processing,
                         "p2p": False, "channel": a.channel}}
    def save():
        (rdir / "meta.json").write_text(json.dumps(meta, indent=2, default=str))
    save()
    log(f"Run folder: {rdir}")

    lock = open(LOCK, "w")
    import fcntl
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        meta["status"] = "failed"; meta["error"] = "another run is in progress"; save()
        sys.exit("Another run is in progress; only one call at a time.")

    class Interrupted(Exception):
        pass
    def on_signal(signum, frame):
        raise Interrupted(f"stopped by signal {signal.Signals(signum).name}")
    for sg in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        if signal.getsignal(sg) != signal.SIG_IGN:      # respect nohup / background jobs
            signal.signal(sg, on_signal)

    procs = {}
    pulse_dir = Path(f"/run/mci-pulse-{run_id}")
    env = dict(os.environ)
    t0 = time.time()
    try:
        # 1. prepare audio
        meta["clip_sha256"] = sha256(clip)
        stim, layout = build_stimulus(decode(clip, a.channel))
        sf.write(rdir / "stimulus.wav", stim, SR, subtype="PCM_16")
        meta["layout"] = layout
        meta["stimulus_sha256"] = sha256(rdir / "stimulus.wav")
        log(f"Stimulus ready: {layout['total_s']:.1f} s (clip starts at {layout['clip_start_s']} s)")
        save()

        # 2. private PulseAudio for this run
        pulse_dir.mkdir(mode=0o755)
        sock = pulse_dir / "native"
        env.update(PULSE_RUNTIME_PATH=str(pulse_dir), PULSE_SERVER=f"unix:{sock}")
        subprocess.run(["pulseaudio", "--daemonize=yes", "-n", "--exit-idle-time=-1",
                        f"--log-target=file:{rdir / 'logs' / 'pulseaudio.log'}",
                        f"--load=module-native-protocol-unix auth-anonymous=1 socket={sock}"],
                       env=env, check=True, capture_output=True)
        for _ in range(50):
            if subprocess.run(["pactl", "info"], env=env, capture_output=True).returncode == 0:
                break
            time.sleep(0.1)
        pa = lambda *x: subprocess.run(["pactl", *x], env=env, check=True, capture_output=True)
        pa("load-module", "module-null-sink", "sink_name=mic", "rate=48000", "channels=1")
        pa("load-module", "module-remap-source", "master=mic.monitor", "source_name=vmic",
           "source_properties=device.description=mci-virtual-mic")
        pa("load-module", "module-null-sink", "sink_name=spk", "rate=48000", "channels=1")
        pa("set-default-source", "vmic")
        pa("set-default-sink", "spk")
        log("Fake mic and speaker ready")

        # 3. fake screen
        disp = next(n for n in range(90, 200) if not Path(f"/tmp/.X11-unix/X{n}").exists())
        procs["xvfb"] = subprocess.Popen(["Xvfb", f":{disp}", "-screen", "0", "1280x720x24", "-nolisten", "tcp"],
                                         stdout=subprocess.DEVNULL, stderr=open(rdir / "logs" / "xvfb.log", "w"))
        env["DISPLAY"] = f":{disp}"
        for _ in range(50):
            if Path(f"/tmp/.X11-unix/X{disp}").exists():
                break
            time.sleep(0.1)

        # 4. start recording the sender's cable
        tlog = rdir / "logs" / "tcpdump.log"
        procs["tcpdump"] = subprocess.Popen(
            ["tcpdump", "-i", CABLE, "-n", "--time-stamp-precision=nano", "-Z", "root",
             "-w", str(rdir / "uplink.pcap")], stdout=subprocess.DEVNULL, stderr=open(tlog, "w"))
        if not wait_for_file_text(tlog, "listening on", 10):
            raise RuntimeError("tcpdump did not start; see logs/tcpdump.log")
        log(f"Recording {CABLE}")

        # 5. the call
        from playwright.sync_api import sync_playwright
        room = "mci-" + run_id.lower()
        meta["room"] = room
        args = ["--no-sandbox", "--use-fake-ui-for-media-stream",
                "--autoplay-policy=no-user-gesture-required"]
        with sync_playwright() as p:
            rb = p.chromium.launch(headless=False, env=env, ignore_default_args=["--mute-audio"],
                                   args=args + ["--use-fake-device-for-media-stream"])
            rpage = rb.new_page()
            rpage.goto(jitsi_url(room, "receiver", False, a.dtx, not a.no_audio_processing), timeout=60000)
            rpage.wait_for_function(JOINED, timeout=60000)
            log("Receiver joined")

            sb = p.chromium.launch(headless=False, env=env, executable_path=WRAPPER,
                                   ignore_default_args=["--mute-audio"], args=args)
            meta["chromium_version"] = sb.version
            spage = sb.new_page()
            clog = open(rdir / "logs" / "sender_console.log", "w")
            spage.on("console", lambda m: clog.write(f"{time.time():.3f} [{m.type}] {m.text}\n"))
            spage.add_init_script(RECORD_PCS)
            spage.goto(jitsi_url(room, "sender", True, a.dtx, not a.no_audio_processing), timeout=60000)
            spage.wait_for_function(JOINED, timeout=60000)
            spage.wait_for_function(f"() => ({MEMBERS})() >= 2", timeout=60000)
            log("Sender joined (inside mcisend); 2 people in call")

            spage.evaluate("() => APP.conference.muteAudio(false)")
            spage.wait_for_function(f"() => !APP.conference.isLocalAudioMuted() && ({MIC_LABEL})() !== null",
                                    timeout=20000)
            meta["sender_mic"] = spage.evaluate(MIC_LABEL)
            meta["effective_config"] = spage.evaluate(EFFECTIVE_CFG)
            log(f"Sender unmuted, mic = {meta['sender_mic']}")
            save()

            procs["parecord"] = subprocess.Popen(
                ["parecord", "--device=spk.monitor", "--latency-msec=20", "--rate=48000", "--channels=1",
                 "--file-format=wav",
                 str(rdir / "received.wav")], env=env)
            meta["t_rec_start_ns"] = time.time_ns()     # receiver recording started
            log(f"Settling {a.settle:.0f} s")
            time.sleep(a.settle)

            meta["t_play_ns"] = time.time_ns()
            log("Playing stimulus")
            subprocess.run(["paplay", "--device=mic", str(rdir / "stimulus.wav")], env=env, check=True)
            meta["t_play_end_ns"] = time.time_ns()
            time.sleep(2)

            (rdir / "sender_webrtc.json").write_text(json.dumps(spage.evaluate(DUMP_PCS), indent=1))
            spage.evaluate("() => { try { APP.conference.hangup(false); } catch (e) {} }")
            time.sleep(0.5)
            sb.close(); rb.close(); clog.close()
        log("Call ended")
        meta["status"] = "ok"
    except Exception as e:
        meta["status"] = "failed"
        meta["error"] = f"{type(e).__name__}: {e}"
        (rdir / "logs" / "error.txt").write_text(traceback.format_exc())
        log(f"FAILED: {meta['error']}")
    finally:
        # 6. clean up, in reverse order, whatever happened
        stop(procs.get("parecord"))
        time.sleep(0.5)
        stop(procs.get("tcpdump"))
        stop(procs.get("xvfb"), signal.SIGTERM)
        for sg in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
            signal.signal(sg, signal.SIG_IGN)       # finish cleanup undisturbed
        if "PULSE_SERVER" in env:
            pid_file = pulse_dir / "pid"
            pid = pid_file.read_text().strip() if pid_file.exists() else None
            subprocess.run(["pactl", "exit"], env=env, capture_output=True)
            for _ in range(50):                      # up to 5 s
                if not pid or not Path(f"/proc/{pid}").exists():
                    break
                time.sleep(0.1)
            else:
                subprocess.run(["kill", "-9", pid], capture_output=True)
        shutil.rmtree(pulse_dir, ignore_errors=True)

    # 7. checks and metadata
    pcap = rdir / "uplink.pcap"
    if pcap.exists():
        meta["pcap_sha256"] = sha256(pcap)
        meta["checks"] = check_pcap(pcap, meta)
    recv = rdir / "received.wav"
    if recv.exists() and recv.stat().st_size > 1000:
        x, _ = sf.read(recv, dtype="float32")
        meta.setdefault("checks", {})["received_peak"] = round(float(np.max(np.abs(x))) if x.size else 0.0, 4)
    if meta["status"] == "ok" and meta.get("checks", {}).get("problems"):
        meta["status"] = "suspect"
    meta["versions"] = {
        "jitsi_packages": out(["dpkg-query", "-W", "-f=${Package}=${Version} ",
                               "jitsi-meet", "jitsi-meet-web", "jicofo", "jitsi-videobridge2"]),
        "kernel": out(["uname", "-r"]), "python": sys.version.split()[0],
        "playwright": out([sys.executable, "-m", "playwright", "--version"]),
        "tcpdump": (out(["tcpdump", "--version"]).splitlines() or [""])[0]}
    meta["duration_s"] = round(time.time() - t0, 1)
    save()
    log(f"Status: {meta['status']}  ->  {rdir}")
    print(json.dumps(meta.get("checks", {}), indent=2))
    lock.close()
    sys.exit(0 if meta["status"] == "ok" else 1)

def check_pcap(pcap, meta):
    """Count the sender's audio packets and flag obvious problems."""
    import dpkt
    hdr = {1: 14, 113: 16, 276: 20}
    udp_up = udp_down = other = 0
    first = last = None
    with open(pcap, "rb") as f:
        r = dpkt.pcap.Reader(f)
        skip = hdr.get(r.datalink(), 14)
        for ts, buf in r:
            raw = buf[skip:]
            try:
                ip = dpkt.ip.IP(raw) if raw and raw[0] >> 4 == 4 else dpkt.ip6.IP6(raw)
            except Exception:
                other += 1; continue
            u = ip.data
            if isinstance(u, dpkt.udp.UDP) and u.dport == 10000:
                udp_up += 1
                first = first or float(ts); last = float(ts)
            elif isinstance(u, dpkt.udp.UDP) and u.sport == 10000:
                udp_down += 1
            else:
                other += 1
    c = {"audio_packets_to_videobridge": udp_up, "packets_from_videobridge": udp_down,
         "other_packets": other, "problems": []}
    play_s = (meta.get("t_play_end_ns", 0) - meta.get("t_play_ns", 0)) / 1e9
    if play_s > 0 and udp_up < 40 * play_s:          # expect ~50 per second
        c["problems"].append(f"only {udp_up} audio packets for {play_s:.1f} s of playback")
    return c

if __name__ == "__main__":
    main()
