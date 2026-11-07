#!/usr/bin/env python3
"""run_batch.py - send many clips through the Jitsi pipeline, one after another.

Usage:
  /opt/mci-venv/bin/python /opt/iconect/run_batch.py MANIFEST.csv --name NAME [options]

MANIFEST.csv has a header row. Only clip_path is required:
  clip_path,clip_id,channel,audio_processing,dtx
  /srv/iconect/clips/P001_week1.wav,P001_w1,,on,off
  /srv/iconect/clips/P002_week1.wav,,0,,
Empty cells use the defaults: clip_id = file name without extension,
channel = all channels mixed, audio_processing = on, dtx = off.
Lines starting with # are ignored.

What it does:
  1. Checks EVERY row before starting (file exists, can be decoded, length,
     clip_id is safe and unique). Refuses to start if anything is wrong.
  2. Skips clips that already have a successful run with the same settings,
     so a batch can be stopped and started again ("resume").
  3. Runs each remaining clip with run_clip.py, retrying failures.
  4. Keeps a live status table (status.csv) and log (batch.log) in
     /srv/iconect/batches/NAME/.
To stop safely after the current clip:  touch /srv/iconect/batches/NAME/STOP
"""
import argparse, csv, json, re, shutil, subprocess, sys, time
from pathlib import Path

PY = "/opt/mci-venv/bin/python"
RUN_CLIP = "/opt/iconect/run_clip.py"
RUNS_ROOT = Path("/srv/iconect/runs")
BATCH_ROOT = Path("/srv/iconect/batches")
SETUP_S = 34                       # pipeline overhead per clip, for time estimates
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
ONOFF = {"": None, "on": True, "off": False, "yes": True, "no": False, "true": True, "false": False}


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def hms(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


def probe_seconds(path):
    """Length of an audio file in seconds via ffprobe, or None if it can't be decoded."""
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def read_manifest(path, defaults):
    rows, problems = [], []
    with open(path, newline="") as f:
        lines = [l for l in f if l.strip() and not l.lstrip().startswith("#")]
    reader = csv.DictReader(lines)
    if not reader.fieldnames or "clip_path" not in [h.strip() for h in reader.fieldnames]:
        sys.exit("MANIFEST ERROR: the first line must be a header containing clip_path")
    for n, raw in enumerate(reader, start=2):
        r = {k.strip(): (v or "").strip() for k, v in raw.items() if k}
        clip = Path(r.get("clip_path", ""))
        cid = r.get("clip_id") or clip.stem
        ap = ONOFF.get(r.get("audio_processing", "").lower(), "bad")
        dtx = ONOFF.get(r.get("dtx", "").lower(), "bad")
        ch = r.get("channel", "")
        row = dict(line=n, clip_path=str(clip), clip_id=cid,
                   channel=int(ch) if ch.isdigit() else None,
                   audio_processing=defaults["audio_processing"] if ap is None else ap,
                   dtx=defaults["dtx"] if dtx is None else dtx)
        bad = []
        if not r.get("clip_path"):
            bad.append("clip_path is empty")
        elif not clip.is_file():
            bad.append("file not found")
        else:
            secs = probe_seconds(clip)
            if secs is None:
                bad.append("file can't be decoded as audio")
            elif secs < 1:
                bad.append(f"too short ({secs:.2f} s)")
            row["seconds"] = secs
        if not SAFE_ID.match(cid):
            bad.append(f"clip_id '{cid}' has unsafe characters (use letters, digits, . _ -)")
        if ch and not ch.isdigit():
            bad.append(f"channel '{ch}' is not a number")
        if ap == "bad" or dtx == "bad":
            bad.append("audio_processing / dtx must be on or off")
        for b in bad:
            problems.append(f"line {n} ({clip.name or '?'}): {b}")
        rows.append(row)
    seen = {}
    for row in rows:
        if row["clip_id"] in seen:
            problems.append(f"line {row['line']}: clip_id '{row['clip_id']}' also used on line {seen[row['clip_id']]}")
        seen.setdefault(row["clip_id"], row["line"])
    return rows, problems


def settings_match(meta, row):
    s = meta.get("settings", {})
    return (s.get("browser_audio_processing", True) == row["audio_processing"]
            and s.get("opus_dtx", False) == row["dtx"]
            and s.get("channel") == row["channel"])


def existing_ok_run(row):
    """Newest earlier run of this clip with status ok and the same settings, or None."""
    folder = RUNS_ROOT / row["clip_id"]
    for meta_path in sorted(folder.glob("*/meta.json"), reverse=True):
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            continue
        if meta.get("status") == "ok" and settings_match(meta, row) \
                and Path(meta.get("clip_path", "")).resolve() == Path(row["clip_path"]).resolve():
            return meta_path.parent
    return None


def run_once(row, log_path):
    """One call through run_clip.py. Returns (status, run_folder)."""
    cmd = [PY, RUN_CLIP, row["clip_path"], "--clip-id", row["clip_id"]]
    if row["channel"] is not None:
        cmd += ["--channel", str(row["channel"])]
    if not row["audio_processing"]:
        cmd.append("--no-audio-processing")
    if row["dtx"]:
        cmd.append("--dtx")
    with open(log_path, "a") as lf:
        lf.write(f"\n===== {now()}  {' '.join(cmd)}\n")
        lf.flush()
        subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT)
    run_dir = None
    for line in open(log_path, errors="ignore"):
        m = re.search(r"Run folder: (\S+)", line)
        if m:
            run_dir = Path(m[1])                     # the last one = this attempt
    if not run_dir or not (run_dir / "meta.json").exists():
        return "failed", run_dir
    try:
        return json.loads((run_dir / "meta.json").read_text()).get("status", "failed"), run_dir
    except ValueError:
        return "failed", run_dir


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manifest", help="CSV file listing the clips")
    ap.add_argument("--name", required=True, help="batch name (folder under /srv/iconect/batches)")
    ap.add_argument("--retries", type=int, default=2, help="extra attempts for a failed clip (default 2)")
    ap.add_argument("--no-audio-processing", action="store_true", help="default for rows that leave it empty")
    ap.add_argument("--dtx", action="store_true", help="default for rows that leave it empty")
    ap.add_argument("--check-only", action="store_true", help="only check the manifest, run nothing")
    ap.add_argument("--min-free-gb", type=float, default=5.0, help="stop if free disk space falls below this")
    a = ap.parse_args()
    if not SAFE_ID.match(a.name):
        sys.exit("--name may only use letters, digits, . _ -")

    rows, problems = read_manifest(a.manifest, {"audio_processing": not a.no_audio_processing, "dtx": a.dtx})
    total_s = sum(r.get("seconds") or 0 for r in rows)
    print(f"Manifest: {len(rows)} clips, {total_s / 60:.1f} min of audio")
    if problems:
        print(f"\n{len(problems)} PROBLEM(S) - nothing was run:")
        for p in problems:
            print("  ", p)
        sys.exit(2)
    todo = [r for r in rows if not existing_ok_run(r)]
    est = sum(SETUP_S + (r.get("seconds") or 0) for r in todo)
    print(f"Already done (ok run with same settings): {len(rows) - len(todo)}")
    print(f"To run: {len(todo)} clips, about {hms(est)} without retries")
    if a.check_only:
        print("Check only: all rows look fine.")
        return

    bdir = BATCH_ROOT / a.name
    (bdir / "logs").mkdir(parents=True, exist_ok=True)
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    shutil.copy(a.manifest, bdir / f"manifest-{time.strftime('%Y%m%dT%H%M%S')}.csv")
    stop_file = bdir / "STOP"
    stop_file.unlink(missing_ok=True)
    status_path = bdir / "status.csv"
    blog = open(bdir / "batch.log", "a", buffering=1)

    def say(msg):
        line = f"[{now()}] {msg}"
        print(line, flush=True)
        blog.write(line + "\n")

    status = {}
    if status_path.exists():                         # keep earlier rows (e.g. from an earlier start)
        for r in csv.DictReader(open(status_path)):
            status[r["clip_id"]] = r

    def save_status():
        tmp = status_path.with_suffix(".tmp")
        with open(tmp, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["clip_id", "clip_path", "status", "attempts", "run_dir", "finished"])
            w.writeheader()
            for r in rows:
                if r["clip_id"] in status:
                    w.writerow({k: status[r["clip_id"]].get(k, "") for k in w.fieldnames})
        tmp.replace(status_path)                     # never leaves a half-written file

    for r in rows:
        done = existing_ok_run(r)
        if done:
            status[r["clip_id"]] = dict(clip_id=r["clip_id"], clip_path=r["clip_path"], status="ok",
                                        attempts=status.get(r["clip_id"], {}).get("attempts", "0"),
                                        run_dir=str(done), finished=status.get(r["clip_id"], {}).get("finished", ""))
    save_status()
    say(f"Batch '{a.name}' starting: {len(todo)} to run, {len(rows) - len(todo)} already done")

    t_start, finished = time.time(), 0
    for i, r in enumerate(todo, 1):
        if stop_file.exists():
            say("STOP file found - stopping before the next clip. Start the same command again to resume.")
            break
        free_gb = shutil.disk_usage(RUNS_ROOT).free / 1e9
        if free_gb < a.min_free_gb:
            say(f"Only {free_gb:.1f} GB free disk space - stopping. Free some space, then start again.")
            break
        st, run_dir = "failed", None
        for attempt in range(1, a.retries + 2):
            say(f"({i}/{len(todo)}) {r['clip_id']}: attempt {attempt} ...")
            st, run_dir = run_once(r, bdir / "logs" / f"{r['clip_id']}.log")
            say(f"({i}/{len(todo)}) {r['clip_id']}: {st}  [{run_dir}]")
            if st == "ok":
                break
            if stop_file.exists():
                break
            time.sleep(5)                            # give the system a moment before retrying
        status[r["clip_id"]] = dict(clip_id=r["clip_id"], clip_path=r["clip_path"], status=st,
                                    attempts=str(attempt), run_dir=str(run_dir or ""), finished=now())
        save_status()
        finished += 1
        left = sum(SETUP_S + (x.get("seconds") or 0) for x in todo[i:])
        pace = (time.time() - t_start) / max(1, sum(SETUP_S + (x.get("seconds") or 0) for x in todo[:i]))
        say(f"Progress: {i}/{len(todo)} done this session, about {hms(left * pace)} left")

    counts = {}
    for r in rows:
        s = status.get(r["clip_id"], {}).get("status", "not run")
        counts[s] = counts.get(s, 0) + 1
    say(f"Batch '{a.name}' finished this session after {hms(time.time() - t_start)}. "
        f"Totals: {', '.join(f'{k} {v}' for k, v in sorted(counts.items()))}")
    say(f"Status table: {status_path}")


if __name__ == "__main__":
    main()
