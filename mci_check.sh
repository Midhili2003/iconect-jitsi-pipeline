#!/usr/bin/env bash
# mci_check.sh - one-command health check for the MCI packet-capture setup.
# Usage:  bash /opt/iconect/mci_check.sh
# Prints OK / WARN / FAIL for each part. Changes nothing.

PY=/opt/mci-venv/bin/python
DOMAIN=iconect-jitsi.hdanny.org
SERVER_IP=143.198.163.242
fails=0; warns=0
ok()   { printf '  OK    %s\n' "$1"; }
warn() { printf '  WARN  %s\n' "$1"; warns=$((warns+1)); }
fail() { printf '  FAIL  %s\n' "$1"; fails=$((fails+1)); }

echo "== Services"
for s in prosody jicofo jitsi-videobridge2 coturn mci-netns; do
  if systemctl is-active --quiet "$s"; then ok "$s running"; else fail "$s not running (try: systemctl restart $s)"; fi
done
if systemctl list-unit-files nginx.service >/dev/null 2>&1 && systemctl cat nginx >/dev/null 2>&1; then
  if systemctl is-active --quiet nginx; then ok "nginx running"; else fail "nginx not running"; fi
fi

echo "== Sender network space (mcisend)"
if ip netns list | grep -qw mcisend; then ok "mcisend exists"; else fail "mcisend missing (try: systemctl restart mci-netns)"; fi
if ip -n mcisend addr show veth-mci1 2>/dev/null | grep -q '10.200.1.2/'; then ok "sender address 10.200.1.2"; else fail "sender address missing"; fi
if grep -q "$SERVER_IP $DOMAIN" /etc/netns/mcisend/hosts 2>/dev/null; then ok "sender finds $DOMAIN"; else fail "/etc/netns/mcisend/hosts wrong"; fi
code=$(ip netns exec mcisend curl -sk -o /dev/null -w '%{http_code}' --max-time 5 "https://$DOMAIN/" 2>/dev/null)
if [ "$code" = "200" ]; then ok "Jitsi web page reachable from the sender (HTTP 200)"; else fail "Jitsi web page not reachable from the sender (got '$code')"; fi

echo "== Browser"
if [ -x /usr/local/bin/chrome-mcisend ]; then
  target=$(grep -o '/[^ ]*chrome-linux64/chrome' /usr/local/bin/chrome-mcisend | head -1)
  if [ -x "$target" ]; then ok "chrome-mcisend -> $target"
  else fail "chrome-mcisend points to a missing browser ($target) - Playwright was probably updated; see README"; fi
else fail "/usr/local/bin/chrome-mcisend missing"; fi

echo "== Programs"
for t in tcpdump ffmpeg ffprobe pulseaudio pactl parecord paplay Xvfb espeak-ng curl; do
  if command -v "$t" >/dev/null; then ok "$t"; else fail "$t not installed"; fi
done
if $PY -c "import playwright, dpkt, numpy, soundfile" 2>/dev/null; then ok "Python libraries (playwright, dpkt, numpy, soundfile)"
else fail "Python libraries missing in /opt/mci-venv"; fi
for f in run_clip.py validate.py inspect_run.py run_batch.py extract_pauses.py make_test_clips.py; do
  if [ -f /opt/iconect/$f ] && $PY -m py_compile /opt/iconect/$f 2>/dev/null; then ok "$f"; else fail "$f missing or broken"; fi
done

echo "== Right now"
if pgrep -f run_batch.py >/dev/null; then warn "a batch is running: $(pgrep -fa run_batch.py | head -1 | cut -c1-120)"; fi
if pgrep -f run_clip.py >/dev/null; then warn "a call is in progress (don't start another)"
elif ! flock -n /run/mci-run.lock true 2>/dev/null; then warn "run lock is held but no run found"
else ok "no call in progress"; fi
left=$(ls -d /run/mci-pulse-* 2>/dev/null | wc -l)
if [ "$left" -gt 0 ] && ! pgrep -f run_clip.py >/dev/null; then warn "$left leftover /run/mci-pulse-* folder(s) (safe to delete when no call is running)"; fi
free_gb=$(df -BG --output=avail /srv | tail -1 | tr -dc 0-9)
if [ "$free_gb" -ge 20 ]; then ok "${free_gb} GB free on /srv"
elif [ "$free_gb" -ge 5 ]; then warn "only ${free_gb} GB free on /srv"
else fail "only ${free_gb} GB free on /srv (batches stop below 5 GB)"; fi
if [ -f /var/run/reboot-required ]; then warn "system restart required (do it when no batch is running)"; fi

echo
echo "Summary: $fails FAIL, $warns WARN"
[ "$fails" -eq 0 ]
