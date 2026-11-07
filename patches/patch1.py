import sys
p = sys.argv[1]
s = open(p).read()
edits = [
 # 1. a stop signal (SSH drop, kill, Ctrl+C) becomes an error, so cleanup still runs
 ('''    procs = {}
    pulse_dir = Path(f"/run/mci-pulse-{run_id}")''',
  '''    class Interrupted(Exception):
        pass
    def on_signal(signum, frame):
        raise Interrupted(f"stopped by signal {signal.Signals(signum).name}")
    for sg in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(sg, on_signal)

    procs = {}
    pulse_dir = Path(f"/run/mci-pulse-{run_id}")'''),
 # 2. wait for PulseAudio to really exit before deleting its folder
 ('''        if "PULSE_SERVER" in env:
            subprocess.run(["pactl", "exit"], env=env, capture_output=True)
        shutil.rmtree(pulse_dir, ignore_errors=True)''',
  '''        for sg in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
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
        shutil.rmtree(pulse_dir, ignore_errors=True)'''),
]
for old, new in edits:
    n = s.count(old)
    if n != 1:
        sys.exit(f"PATCH FAILED: expected 1 match, found {n} for:\n{old[:80]}")
    s = s.replace(old, new)
open(p, "w").write(s)
print("patched OK")
