#!/bin/bash
# Builds (up) or removes (down) the sender's private network room.
set -e
NS=mcisend
case "$1" in
  up)
    ip netns list | grep -qw "$NS" || ip netns add "$NS"
    if ! ip link show veth-mci0 >/dev/null 2>&1; then
      ip link add veth-mci0 type veth peer name veth-mci1
      ip link set veth-mci1 netns "$NS"
      ip addr add 10.200.1.1/24 dev veth-mci0
      ip netns exec "$NS" ip addr add 10.200.1.2/24 dev veth-mci1
    fi
    ip link set veth-mci0 up
    ip netns exec "$NS" ip link set veth-mci1 up
    ip netns exec "$NS" ip link set lo up
    ip netns exec "$NS" ip route replace default via 10.200.1.1
    ethtool -K veth-mci0 gro off gso off tso off
    ip netns exec "$NS" ethtool -K veth-mci1 gro off gso off tso off
    mkdir -p "/etc/netns/$NS"
    printf '127.0.0.1 localhost\n143.198.163.242 iconect-jitsi.hdanny.org\n' > "/etc/netns/$NS/hosts"
    ;;
  down)
    ip link del veth-mci0 2>/dev/null || true
    ip netns del "$NS" 2>/dev/null || true
    ;;
  *)
    echo "usage: $0 up|down"; exit 1 ;;
esac
