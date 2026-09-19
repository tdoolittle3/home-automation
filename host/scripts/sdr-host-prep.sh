#!/usr/bin/env bash
# sdr-host-prep.sh — one-time host preparation for the RTL-SDR dongle.
#
#   sudo bash host/scripts/sdr-host-prep.sh
#
# Idempotent: safe to re-run. Makes three changes, all reversible (see the
# "Backing this out" section in docs/sdr.md):
#   1. installs rtl-sdr CLI tools (rtl_test, rtl_eeprom, rtl_tcp)
#   2. blacklists the DVB-T kernel driver so it stops claiming the dongle
#   3. adds a udev rule granting plugdev access to the device
#
# Touches nothing belonging to the existing stacks.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "error: must run as root — try: sudo bash $0" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

say "1/5  Installing rtl-sdr tools"
if ! command -v rtl_test >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y rtl-sdr librtlsdr0
else
    echo "already installed: $(command -v rtl_test)"
fi

say "2/5  Blacklisting the DVB-T driver"
install -m 0644 "$REPO_ROOT/host/etc/modprobe.d/blacklist-rtl-sdr.conf" \
                /etc/modprobe.d/blacklist-rtl-sdr.conf
echo "wrote /etc/modprobe.d/blacklist-rtl-sdr.conf"

# The blacklist governs future loads; anything loaded right now has to be
# evicted by hand or the dongle stays claimed until the next reboot.
for mod in dvb_usb_rtl28xxu rtl2832_sdr rtl2832 rtl2830; do
    if lsmod | grep -q "^${mod}\b"; then
        echo "unloading currently-loaded module: $mod"
        modprobe -r "$mod" 2>/dev/null || echo "  (busy — will clear on reboot)"
    fi
done

# The blacklist lives in the initramfs too. Skipping this is the classic
# failure: everything looks right, but the driver still binds at early boot.
say "3/5  Rebuilding initramfs (picks up the blacklist)"
update-initramfs -u

say "4/5  Installing the udev rule"
install -m 0644 "$REPO_ROOT/host/etc/udev/rules.d/99-rtl-sdr.rules" \
                /etc/udev/rules.d/99-rtl-sdr.rules
udevadm control --reload-rules
# A plain `udevadm trigger` does NOT re-apply permissions to an already
# enumerated USB device — it has to be targeted at the usb subsystem with
# an explicit add action.
udevadm trigger --subsystem-match=usb --action=add
echo "wrote and re-triggered /etc/udev/rules.d/99-rtl-sdr.rules"

say "5/5  Verifying"
echo "--- lsusb (looking for 0bda:2838 / 0bda:2832) ---"
if lsusb | grep -iE '0bda:(2838|2832)|RTL2838|RTL2832'; then
    echo
    echo "--- rtl_test -t (Ctrl-C after a few seconds if it keeps scanning) ---"
    timeout 15 rtl_test -t || true
else
    lsusb
    echo
    echo "!! No RTL-SDR found on the USB bus."
    echo "   Plug the dongle into ladybird and re-run this script."
    exit 1
fi

cat <<'DONE'

==> Host prep complete.

Next:
  cd /opt/stacks/sdr
  cp .env.example .env && ${EDITOR:-nano} .env    # fill in real lat/lon/alt
  docker compose up -d
  docker compose logs -f ultrafeeder

If rtl_test still reports the device is busy, reboot — the initramfs
blacklist only takes effect on the next boot.
DONE
