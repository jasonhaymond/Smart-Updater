"""Windows (winget) backend.

EXPERIMENTAL: written against winget's documented behaviour with fixture-tested
table parsing, but not yet run on a real Windows machine.

Limits worth knowing:
  * winget has no dry run, so ``simulate`` cannot check dependencies or
    removals. Safety here comes from policy (version-jump limits, rules),
    verification after each update, and rollback.
  * winget's output is a localised, width-formatted table, so this backend
    expects English output. If the table is cut off or unrecognised it reports
    that rather than guessing.
"""
from __future__ import annotations

import os
import re
import shutil
from typing import Any, Dict, List, Optional, Set, Tuple

from ..models import ApplyResult, SimResult, Update
from .base import Backend, BackendError

_COMMON = ["--accept-source-agreements", "--disable-interactivity"]
_INSTALL_FLAGS = ["--silent", "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"]
_NOTHING = re.compile(r"No installed package found|No applicable upgrade|No available upgrade", re.I)
_SUMMARY = re.compile(r"^\d+\s+(upgrades?|packages?)\s+available", re.I)


def parse_table(text: str) -> Optional[List[Dict[str, str]]]:
    """Parse the first table in winget output into rows keyed by header name.

    Columns are located by the header's character offsets. Returns None if no
    table is present.
    """
    lines = [l.rstrip("\r") for l in text.splitlines()]
    for i in range(len(lines) - 1):
        sep = lines[i + 1].strip()
        if len(sep) >= 3 and set(sep) == {"-"} and lines[i].strip():
            header = lines[i]
            cols = [(m.start(), m.group()) for m in re.finditer(r"\S+(?: \S+)*", header)]
            rows: List[Dict[str, str]] = []
            for line in lines[i + 2 :]:
                if not line.strip() or _SUMMARY.match(line.strip()):
                    break
                row = {}
                for idx, (start, name) in enumerate(cols):
                    end = cols[idx + 1][0] if idx + 1 < len(cols) else None
                    row[name] = line[start:end].strip()
                rows.append(row)
            return rows
    return None


def rows_to_updates(rows: List[Dict[str, str]], backend_id: str = "winget"):
    updates: List[Update] = []
    skipped: List[str] = []
    for row in rows:
        pid, name = row.get("Id", ""), row.get("Name", "")
        ver, avail = row.get("Version", ""), row.get("Available", "")
        if not pid or not avail:
            continue
        if pid.endswith("…") or avail.endswith("…") or ver.endswith("…"):
            skipped.append(f"{name or pid}: output was truncated, id/version unreliable")
            continue
        if not ver or ver.lower().startswith("unknown") or ver[:1] in ("<", ">"):
            skipped.append(f"{name or pid}: installed version is '{ver}', cannot compare safely")
            continue
        updates.append(
            Update(backend=backend_id, name=pid, display=name, installed=ver, candidate=avail, source=row.get("Source", ""))
        )
    return updates, skipped


class WingetBackend(Backend):
    NAME = "winget"
    scheme = "generic"
    stable_release = False
    rollback_support = "best_effort"  # needs the old version to still be available from the source
    experimental = True
    can_simulate = False
    default_max_bump = "major"
    skipped: List[str] = []  # rows winget listed that we refused to act on, with the reason

    @classmethod
    def available(cls) -> bool:
        return os.name == "nt" and bool(shutil.which("winget"))

    def refresh(self) -> bool:
        return self.run(["winget", "source", "update", *(_COMMON)], label="winget-source-update", timeout=300).ok

    def list_updates(self) -> List[Update]:
        r = self.run(["winget", "upgrade", *_COMMON], label="winget-upgrade", timeout=300)
        rows = parse_table(r.stdout)
        if rows is None:
            if _NOTHING.search(r.stdout):
                return []
            raise BackendError(
                f"could not find a package table in 'winget upgrade' output (rc={r.rc}). "
                "Non-English Windows or an unexpected format; see the command log."
            )
        updates, skipped = rows_to_updates(rows, self.id)
        for s in skipped:
            self.events.emit("winget_row_skipped", s, level="warning")
        self.skipped = skipped
        return updates

    def held(self) -> Set[str]:
        r = self.run(["winget", "pin", "list"], label="winget-pin-list")
        rows = parse_table(r.stdout) if r.ok else None
        return {row["Id"] for row in rows or [] if row.get("Id")}

    def hold_hint(self, name: str) -> str:
        return f"winget pin remove --id {name}"

    def simulate(self, targets: List[Update]) -> SimResult:
        # No dry run exists; report what we intend to do, flagged as unanalysed.
        return SimResult(
            True,
            upgrades={t.name: t.candidate for t in targets},
            previous={t.name: t.installed for t in targets},
            analyzed=False,
        )

    def installed_versions(self, names: List[str]) -> Dict[str, str]:
        found: Dict[str, str] = {}
        for pid in names:
            r = self.run(["winget", "list", "--id", pid, "--exact", *_COMMON], label="winget-list")
            rows = parse_table(r.stdout) if r.ok else None
            for row in rows or []:
                if row.get("Id") == pid and row.get("Version"):
                    found[pid] = row["Version"]
        return found

    def _run_each(self, args_for, label: str, pairs: Dict[str, str]) -> ApplyResult:
        failures: List[str] = []
        last_log = ""
        timeout = self.options.get("apply_timeout", 3600)
        for pid, ver in pairs.items():
            r = self.run(args_for(pid, ver), label=f"{label}-{pid}", timeout=timeout, mutating=True)
            last_log = r.log_path or last_log
            if not r.ok:
                failures.append(f"{pid}: winget exit code 0x{r.rc & 0xFFFFFFFF:08X}")
        return ApplyResult(not failures, 0 if not failures else 1, "; ".join(failures), log_path=last_log)

    def apply(self, sim: SimResult) -> ApplyResult:
        return self._run_each(
            lambda pid, ver: ["winget", "upgrade", "--id", pid, "--exact", "--version", ver, *_INSTALL_FLAGS],
            "winget-apply",
            sim.upgrades,
        )

    def rollback(self, snapshot: Dict[str, Any]) -> ApplyResult:
        return self._run_each(
            lambda pid, ver: ["winget", "install", "--id", pid, "--exact", "--version", ver, "--force", *_INSTALL_FLAGS],
            "winget-rollback",
            {n: v for n, v in snapshot.get("versions", {}).items() if v},
        )
