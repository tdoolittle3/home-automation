#!/bin/sh
# Privacy shutter on the indoor SimpliCam, for the command_line switch in
# configuration.yaml (switch.simplicam_shutter).
#
#   simplicam_shutter.sh open|close|status   ->  prints open | closed | unknown
#
# The camera is rooted and its only remote door is a root telnet shell with no
# login (LAN only). This script is the one thing that talks to it: it runs the
# camera's own /opt/simplisafe/camera/bin/shutter.sh with a fixed argument and
# nothing else - no caller input ever reaches the camera's shell.
#
# The shutter has no position sensor. "status" is the last state commanded
# through shutter.sh, kept in the camera's /tmp, so it reads "unknown" after a
# camera reboot and is wrong if SimpliSafe's own app moved the shutter since.
# The picture is the truth: closed = black frames.
#
# Exit 0 = the camera answered; exit 1 = it did not (HA keeps the last state).
HOST="${SIMPLICAM_HOST:-192.168.0.26}"

case "$1" in
  open|close|status) ;;
  *) echo "usage: $0 open|close|status" >&2; exit 2 ;;
esac

# telnetd wants a moment before it reads, and nc must stay open long enough to
# carry the answer back (open/close hold the motor for half a second).
reply=$(
  {
    sleep 1
    printf '/opt/simplisafe/camera/bin/shutter.sh %s; echo SHUTTER_"DONE"\r\n' "$1"
    sleep 2
  } | nc -w 5 "$HOST" 23 2>/dev/null | tr -d '\r'
)

# The echoed command line carries SHUTTER_"DONE" (quotes intact); only the real
# output has the bare marker, and the line before it is shutter.sh's answer.
state=$(printf '%s\n' "$reply" | awk '$0 == "SHUTTER_DONE" { print prev; found = 1; exit } { prev = $0 } END { if (!found) exit 1 }') || exit 1

case "$state" in
  open|closed|unknown) echo "$state" ;;
  *) exit 1 ;;
esac
