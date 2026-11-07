# MCI passive monitoring - Jitsi capture pipeline

Plays an audio clip into a real Jitsi call from a virtual microphone, records
the sender's ENCRYPTED packets, and measures speech and pauses from packet
sizes alone. Server: iconect-jitsi.hdanny.org (143.198.163.242).

## Quick start
  bash /opt/iconect/mci_check.sh                       # is everything healthy?
  PY=/opt/mci-venv/bin/python

  # one clip (about 34 s + clip length)
  $PY /opt/iconect/run_clip.py /path/clip.wav --clip-id NAME

  # many clips: write a manifest, check it, run it in the background
  $PY /opt/iconect/run_batch.py /srv/iconect/manifests/NAME.csv --name NAME --check-only
  nohup $PY /opt/iconect/run_batch.py /srv/iconect/manifests/NAME.csv --name NAME \
        > /srv/iconect/batches/NAME-console.log 2>&1 &
  tail -f /srv/iconect/batches/NAME/batch.log         # watch (Ctrl+C stops watching only)
  touch /srv/iconect/batches/NAME/STOP                # stop after the current clip
  # resume = run the same nohup command again (finished clips are skipped)

  # speech/pause timings for a finished batch
  $PY /opt/iconect/extract_pauses.py --batch NAME     # -> batches/NAME/features.csv

## Manifest format (CSV, header required, only clip_path needed)
  clip_path,clip_id,channel,audio_processing,dtx
  /srv/iconect/clips/P001.wav,P001,,on,off
  Defaults: clip_id = file name, channel = all mixed, audio_processing = on, dtx = off.

## Tools in /opt/iconect
  run_clip.py         one call: stimulus -> Jitsi -> packet capture -> run folder
  run_batch.py        many clips: checks, resume, retries, STOP, status.csv
  extract_pauses.py   packets -> pauses.csv + features.json (+ features.csv per batch)
  validate.py         scores accuracy against known audio (test clips)
  inspect_run.py      shows what is inside one run's packets
  make_test_clips.py  makes speech test clips with pauses of known length
  mci_check.sh        health check (changes nothing)
  patches/            patch1-8 and backups of earlier versions (history only)

## Outputs
  /srv/iconect/runs/<clip_id>/<run_id>/
      stimulus.wav   exactly what was played (marker + clip)
      uplink.pcap    sender's encrypted packets (the research data)
      received.wav   what the other side heard (sanity check)
      meta.json      settings, timings, checks, checksums, status (ok/suspect/failed)
      pauses.csv, features.json   from extract_pauses.py
      logs/          browser, tcpdump and pipeline logs
  /srv/iconect/batches/<name>/   batch.log, status.csv, features.csv, logs/

## How it works (short)
  - Sender browser runs inside network space "mcisend" (veth 10.200.1.2), so
    tcpdump on veth-mci0 sees ONLY its traffic. Rebuilt at boot by mci-netns.service.
  - Each run uses its own PulseAudio (fake mic "vmic", silent speaker "spk") and Xvfb screen.
  - Stimulus = 1 s silence, 3 chirps at 1.0/1.5/2.3 s (sync marker), clip from 4.0 s, 1 s tail.
  - Speech is detected from Opus packet sizes; the size threshold is chosen per
    recording with Otsu's method (works with background noise). Validated in
    Phase 6: 160/160 designed pauses found, typical error ~20 ms.
  - Only one call at a time (lock file /run/mci-run.lock).

## Fixing common problems
  - FAIL "chrome-mcisend points to a missing browser": Playwright updated Chromium.
      ls /root/.cache/ms-playwright/          # find the new chromium-XXXX folder
      nano /usr/local/bin/chrome-mcisend      # put the new path after "ip netns exec mcisend"
  - mcisend missing:            systemctl restart mci-netns
  - a Jitsi service not running: systemctl restart <service>; journalctl -u <service> -n 50
  - "Another run is in progress": wait, or check  pgrep -fa run_clip.py
  - Clip "failed" in status.csv: read batches/NAME/logs/<clip_id>.log, then rerun the batch.
  - Long jobs: always start with nohup (closing the window will not stop them).

## Open decisions (for the PI)
  I-CONECT data approval on this server; participant-only vs mixed clips; audio
  cleanup on/off; DTX; whether the unencrypted audio-level header may be used;
  pause cut-off (0.25 s?) and the ~20 ms shortening of measured pauses.

## Setup history
  Phase guides 0-7 (PDFs) describe every command used to build this.
