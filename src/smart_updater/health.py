"""Health snapshots: what 'working' looks like before an update, to compare after.

A snapshot is only ever compared with another snapshot from the same machine,
so checks that were already failing before an update never count against it.
"""
from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class HealthSnapshot:
    custom: Dict[str, bool] = field(default_factory=dict)
    failed_units: List[str] = field(default_factory=list)
    stopped_services: List[str] = field(default_factory=list)
    problems: Dict[str, List[str]] = field(default_factory=dict)

    def to_dict(self):
        return {
            "custom_checks": self.custom,
            "failed_units": self.failed_units,
            "stopped_services": self.stopped_services,
            "package_problems": self.problems,
        }


class Health:
    def __init__(self, cfg, runner, events, backends):
        self.cfg, self.runner, self.events, self.backends = cfg, runner, events, backends

    def _custom(self, hc) -> bool:
        for attempt in range(hc.retries + 1):
            r = self.runner.run(hc.command, label=f"health-{hc.name}", timeout=hc.timeout)
            if r.ok:
                return True
            if attempt < hc.retries:
                time.sleep(hc.retry_delay)
        return False

    def _failed_units(self) -> List[str]:
        if os.name == "nt" or not shutil.which("systemctl"):
            return []
        r = self.runner.run(["systemctl", "--failed", "--no-legend", "--plain", "--no-pager"], label="systemd-failed")
        if not r.ok:
            return []  # e.g. a container without systemd
        return sorted({l.split()[0].lstrip("●* ") for l in r.stdout.splitlines() if l.strip()})

    def _stopped_services(self) -> List[str]:
        if os.name != "nt":
            return []
        script = (
            "Get-Service | Where-Object { $_.StartType -eq 'Automatic' -and $_.Status -ne 'Running' } "
            "| ForEach-Object { $_.Name }"
        )
        r = self.runner.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], label="win-services", timeout=60)
        return sorted({l.strip() for l in r.stdout.splitlines() if l.strip()}) if r.ok else []

    def snapshot(self) -> HealthSnapshot:
        snap = HealthSnapshot()
        for hc in self.cfg.health_checks:
            snap.custom[hc.name] = self._custom(hc)
        snap.failed_units = self._failed_units()
        snap.stopped_services = self._stopped_services()
        for b in self.backends:
            snap.problems[b.id] = b.problems()
        self.events.emit("health_snapshot", "captured", level="debug", **{k: v for k, v in snap.to_dict().items()})
        return snap

    @staticmethod
    def regressions(before: HealthSnapshot, after: HealthSnapshot) -> List[str]:
        out: List[str] = []
        for name, was_ok in before.custom.items():
            if was_ok and not after.custom.get(name, True):
                out.append(f"health check '{name}' passed before and now fails")
        for u in sorted(set(after.failed_units) - set(before.failed_units)):
            out.append(f"systemd unit {u} is now in a failed state")
        for s in sorted(set(after.stopped_services) - set(before.stopped_services)):
            out.append(f"Windows service {s} (automatic start) is no longer running")
        for bid, probs in after.problems.items():
            for p in sorted(set(probs) - set(before.problems.get(bid, []))):
                out.append(f"[{bid}] new package database problem: {p}")
        return out


def windows_reboot_pending() -> List[str]:
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []
    reasons: List[str] = []
    for path, label in (
        (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending", "component servicing"),
        (r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired", "Windows Update"),
    ):
        try:
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path))
            reasons.append(f"pending reboot flagged by {label}")
        except OSError:
            pass
    return reasons
