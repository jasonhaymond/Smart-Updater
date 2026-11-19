#!/bin/sh
# Looksee "Custom script" check for Smart Updater (Linux and macOS).
#
# Put this file in the Looksee agent's script_dir, owned by root and not writable by anyone else
# (the agent runs it): see docs/LOOKSEE.md. Then add a Custom script check named
# smart-updater-check.sh. It prints Smart Updater's status line plus, for anything that needs a
# person, what it is, why, and what to do. The exit code is the Nagios one Looksee understands:
# 0 = up, 1 = warn, 2 = down (critical), 3 = unknown (no report, or the updater has stopped running).
#
# Extra arguments pass through, e.g. a Looksee check with arguments "--max-age-hours 60".
#
# The executable is looked for only in places an ordinary user cannot write. Homebrew's own
# folders are deliberately not searched: on macOS this runs as root.
for candidate in "${SMART_UPDATER_BIN:-}" \
    /usr/local/bin/smart-updater \
    /usr/local/libexec/smart-updater/smart-updater.pyz \
    /opt/smart-updater/smart-updater.pyz
do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
        exec "$candidate" check --details "$@"
    fi
done
echo "SMART_UPDATER UNKNOWN - smart-updater is not installed in a known location (edit this script to point at it)"
exit 3
