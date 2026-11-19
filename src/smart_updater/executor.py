"""The executor: applies planned groups one at a time and proves each one worked.

For every unit of work it:
  1. re-resolves the group against the *current* state (things may have changed
     since planning),
  2. records a snapshot of the previous versions,
  3. applies it,
  4. checks the versions really changed and that health did not get worse,
  5. rolls back if either check fails, and quarantines that version so it is
     not retried every run.

If a batch of several independent groups fails, it is rolled back and the
groups are retried one at a time, so one bad update cannot block the others.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .events import now_iso
from .models import Decision, Reason, Status
from .planner import Group, PlanResult


@dataclass
class Outcome:
    ok: bool
    reason: Reason = Reason.OK
    text: str = ""
    rollback_attempted: bool = False
    rollback_ok: bool = True
    notes: List[str] = field(default_factory=list)


def make_units(groups: List[Group], batch_size: int) -> List[List[Group]]:
    """Coalesce non-overlapping groups into batches of up to ``batch_size``."""
    units: List[List[Group]] = []
    cur: List[Group] = []
    used = set()
    for g in groups:
        if batch_size <= 1:
            units.append([g])
            continue
        if cur and (len(cur) >= batch_size or used & set(g.members)):
            units.append(cur)
            cur, used = [], set()
        cur.append(g)
        used |= set(g.members)
    if cur:
        units.append(cur)
    return units


class Executor:
    def __init__(self, cfg, backend, planner, health, events, state, run_dir, baseline):
        self.cfg, self.backend, self.planner, self.health = cfg, backend, planner, health
        self.events, self.state, self.run_dir = events, state, run_dir
        self.baseline = baseline
        self.decisions: Dict[str, Decision] = {}
        self.snapshots: List[dict] = []

    # ---- public ------------------------------------------------------------
    def run(self, plan: PlanResult) -> None:
        self.decisions = plan.decisions
        failures = 0
        aborted = False
        for unit in make_units(plan.groups, self.cfg.batch_size):
            if aborted:
                self._mark(unit, Status.SKIPPED, Reason.RUN_ABORTED,
                           f"Not attempted: the run stopped after {self.cfg.max_failures} failures in a row.", "warning")
                continue
            for ok in self._apply_unit(unit):
                failures = 0 if ok else failures + 1
            if failures >= self.cfg.max_failures:
                aborted = True
                self.events.emit("run_aborted", f"{failures} consecutive failures; stopping", level="error")

    # ---- one unit ------------------------------------------------------------
    def _apply_unit(self, groups: List[Group]) -> List[bool]:
        roots = [r for g in groups for r in g.roots]
        res = self.planner.resolve(roots)
        if not res.ok:
            if len(groups) > 1:
                results: List[bool] = []
                for g in groups:
                    results += self._apply_unit([g])
                return results
            self._mark(groups, Status.BLOCKED, res.reason,
                       f"Re-checked right before applying and it no longer passes: {res.text}",
                       "warning" if any(r.security for r in roots) else "info", res.action, res.chain)
            return [True]  # nothing was changed, so this is not a failure

        grp = res.group
        out = self._attempt(grp)
        if out.ok:
            self._mark_updated(groups + [grp], grp)
            return [True] * len(groups)
        if len(groups) > 1 and out.rollback_ok:
            self.events.emit("unit_split", f"batch of {len(groups)} failed ({out.reason.value}); retrying one group at a time", level="warning")
            results = []
            for g in groups:
                results += self._apply_unit([g])
            return results
        self._mark_failed(groups + [grp], grp, out)
        return [False] * len(groups)

    def _attempt(self, grp: Group) -> Outcome:
        sim = grp.sim
        names = ", ".join(sorted(grp.members))
        snap = self.backend.snapshot(sim)
        snap.update({"members": sorted(grp.members), "time": now_iso(), "target": dict(sim.upgrades)})
        self.snapshots.append(snap)
        self._persist()
        self.events.emit("unit_start", f"applying: {names}", package_count=len(grp.members))

        res = self.backend.apply(sim)
        snap["token"] = res.token
        self._persist()
        if not res.ok:
            return self._failed(Reason.APPLY_FAILED, f"The package manager reported an error while applying: {res.error or 'rc=' + str(res.rc)}", snap)

        installed = self.backend.installed_versions(list(sim.upgrades))
        unchanged = [n for n, old in snap["versions"].items() if old and installed.get(n) == old and n in sim.upgrades]
        if unchanged:
            return self._failed(Reason.VERSION_MISMATCH, f"The package manager reported success but {', '.join(unchanged)} did not change version.", snap)
        for n, want in sim.upgrades.items():
            got = installed.get(n)
            if got and got != want:
                self.events.emit("version_differs", f"{n}: expected {want}, found {got}", level="warning")

        after = self.health.snapshot()
        regress = self.health.regressions(self.baseline, after)
        if regress:
            return self._failed(Reason.VERIFY_FAILED, "Applied, but health got worse: " + "; ".join(regress), snap)
        self.baseline = after
        self.events.emit("unit_ok", f"verified: {names}")
        return Outcome(True)

    def _failed(self, reason: Reason, text: str, snap: dict) -> Outcome:
        self.events.emit("unit_failed", text, level="error", reason=reason.value)
        out = Outcome(False, reason, text, rollback_attempted=True)
        rb = self.backend.rollback(snap)
        restored = self.backend.installed_versions(list(snap["versions"]))
        not_restored = [n for n, v in snap["versions"].items() if v and restored.get(n) != v]
        residual = self.health.regressions(self.baseline, self.health.snapshot())
        out.rollback_ok = rb.ok and not not_restored and not residual
        if not rb.ok:
            out.notes.append(f"rollback command failed: {rb.error}")
        if not_restored:
            out.notes.append(f"versions not restored: {', '.join(not_restored)}")
        if residual:
            out.notes.append("health still degraded after rollback: " + "; ".join(residual))
        self.events.emit("rollback_done", "ok" if out.rollback_ok else "; ".join(out.notes), level="info" if out.rollback_ok else "error")
        return out

    # ---- recording -------------------------------------------------------------
    def _persist(self) -> None:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", self.backend.id)
        with open(self.run_dir / f"snapshots-{safe}.json", "w", encoding="utf-8") as fh:
            json.dump(self.snapshots, fh, indent=2)

    def _members(self, groups: List[Group]) -> List[Decision]:
        out: Dict[str, Decision] = {}
        for g in groups:
            for name in g.members:
                d = self.decisions.get(f"{self.backend.id}:{name}")
                if d:
                    out[d.key] = d
        return list(out.values())

    def _mark(self, groups, status, reason, text, attention=None, action="", chain=None) -> None:
        for d in self._members(groups):
            d.status, d.reason_code, d.reason = status, reason, text
            d.attention, d.suggested_action = attention, action
            if chain is not None:
                d.chain = list(chain)

    def _mark_updated(self, groups: List[Group], grp: Group) -> None:
        roots = {r.name for g in groups for r in g.roots}
        for d in self._members(groups):
            d.status, d.reason_code, d.attention, d.suggested_action = Status.UPDATED, Reason.OK, None, ""
            d.reason = f"Updated {d.installed} → {d.candidate}. Verified afterwards: versions changed and no new health problems."
            if d.name not in roots:
                d.reason += " Applied as a required dependency of another update."
            else:
                d.chain = list(grp.chain)

    def _mark_failed(self, groups: List[Group], grp: Group, out: Outcome) -> None:
        extra = (" " + " ".join(out.notes)) if out.notes else ""
        if not out.rollback_ok:
            status, reason, attention = Status.FAILED, Reason.ROLLBACK_FAILED, "critical"
            text = f"{out.text.rstrip('.')}. Rolling back did NOT fully work.{extra} The machine may be in a half-updated state."
            action = (
                f"Check the machine now. Command output is in {self.run_dir / 'commands'}; the previous versions are listed in "
                f"{self.run_dir}/snapshots-*.json (restore with: smart-updater rollback {self.run_dir.name})."
            )
        else:
            status = Status.ROLLED_BACK if out.reason == Reason.VERIFY_FAILED else Status.FAILED
            reason, attention = out.reason, "warning"
            text = f"{out.text.rstrip('.')}. Rolled back to the previous versions.{extra}"
            action = (
                f"Find the cause in {self.run_dir / 'commands'}. This version is paused for {self.cfg.quarantine_days:g} days "
                "so it does not fail again every run; clear the pause with: smart-updater unquarantine"
            )
            expiry = time.time() + self.cfg.quarantine_days * 86400
            for g in groups:
                for r in g.roots:
                    self.state.data["quarantine"][f"{r.key}:{r.candidate}"] = expiry
        self._mark(groups, status, reason, text, attention, action, grp.chain)
