"""The engine: one run, start to finish."""
from __future__ import annotations

import contextlib
import json
import os
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from . import __version__, looksee, osv
from .backends import BackendError, detect
from .events import EventLog, now_iso
from .executor import Executor
from .health import Health, windows_reboot_pending
from .models import AttentionItem, Decision, Reason, Status
from .planner import Planner
from .report import RunReport, decision_to_item, finalize_items, compute_status, write_reports
from .runner import Runner
from .state import RunLock, State, default_state_dir, is_privileged
from .version import classify_bump


class EngineError(RuntimeError):
    pass


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + os.urandom(2).hex()


def _need_privilege(what: str) -> None:
    if not is_privileged():
        who = "an administrator (elevated) prompt" if os.name == "nt" else "root (for example with sudo)"
        raise EngineError(f"{what} needs {who}. Use 'plan' to preview without changing anything.")


def run_once(
    cfg,
    mode: str,
    *,
    only: Optional[List[str]] = None,
    refresh: bool = False,
    verbose: bool = False,
    quiet: bool = False,
    state_dir: Optional[str] = None,
) -> RunReport:
    assert mode in ("plan", "apply")
    t0 = time.monotonic()
    root = Path(state_dir or cfg.state_dir or default_state_dir())
    state = State(root)
    run_id = new_run_id()
    events = EventLog(root, run_id, verbose, quiet)
    with contextlib.ExitStack() as stack:
        stack.callback(events.close)
        if mode == "apply":
            _need_privilege("Applying updates")
            stack.enter_context(RunLock(state.lock_path))
        events.emit("run_start", f"config: {cfg.source}", mode=mode, version=__version__)

        runner = Runner(events, events.run_dir, read_only=(mode != "apply"))
        backends = detect(cfg, runner, events, only)
        report = RunReport(run_id=run_id, mode=mode, started=now_iso())
        items: List[AttentionItem] = []
        decisions: List[Decision] = []
        now_ts = time.time()
        state.data["quarantine"] = {k: v for k, v in state.data["quarantine"].items() if v > now_ts}
        health = Health(cfg, runner, events, backends)
        baseline = None

        if not backends:
            msg = "no supported package manager was detected (or all are disabled in the config)"
            report.errors.append(msg)
            items.append(AttentionItem("system:no_backends", "warning", "No package manager detected", Reason.BACKEND_ERROR.value, msg,
                                       "Run 'smart-updater doctor' to see what was looked for."))

        for b in backends:
            info = b.describe()
            info.update(updates_found=0, error=None)
            report.backends.append(info)
            try:
                if mode == "apply" or refresh:
                    if not b.refresh():
                        report.notes.append(f"{b.id}: could not refresh package metadata; used the cached lists")
                        events.emit("refresh_failed", b.id, level="warning")
                updates = b.list_updates()
                info["updates_found"] = len(updates)
                for u in updates:
                    u.bump = classify_bump(u.installed, u.candidate, b.scheme) if u.installed else u.bump
                if b.osv_ecosystem and cfg.osv_enabled:
                    no_fix, ok = osv.classify(b.osv_ecosystem, b.installed_all(), updates)
                    if not ok:
                        report.notes.append(f"{b.id}: vulnerability lookup (OSV.dev) was unreachable, so security status is unknown for this backend")
                        events.emit("osv_unreachable", b.id, level="warning")
                    for nf in no_fix:
                        items.append(AttentionItem(
                            id=f"{b.id}:{nf['name']}:{Reason.NO_FIX_AVAILABLE.value}", severity="warning",
                            title=f"{nf['name']} {nf['installed']}: vulnerable and no fix is available",
                            reason_code=Reason.NO_FIX_AVAILABLE.value,
                            reason=f"Known vulnerabilities {', '.join(nf['advisories'][:5])} affect this version and no newer version fixes them.",
                            suggested_action="Replace the package, remove it, or accept the risk and monitor the advisories.",
                            backend=b.id, package=nf["name"], installed=nf["installed"], candidate=nf["candidate"],
                            security=True, advisories=nf["advisories"], escalate_after_days=cfg.critical_after_days,
                        ))
                for s in getattr(b, "skipped", []):
                    report.notes.append(f"{b.id}: {s}")
                planner = Planner(cfg, b, events, updates, state.data["quarantine"])
                plan = planner.plan()
                if mode == "apply" and plan.groups:
                    if baseline is None:
                        baseline = health.snapshot()
                        for name, ok in baseline.custom.items():
                            if not ok:
                                items.append(AttentionItem(
                                    f"system:health:{name}", "warning", f"Health check '{name}' was already failing",
                                    Reason.HEALTH_BASELINE.value,
                                    "It failed before any update was applied, so it cannot be used to detect regressions.",
                                    "Fix the check or the service it watches."))
                    ex = Executor(cfg, b, planner, health, events, state, events.run_dir, baseline)
                    ex.run(plan)
                    baseline = ex.baseline
                decisions += list(plan.decisions.values())
            except Exception as e:  # noqa: BLE001 - one broken backend must not stop the others
                if not isinstance(e, BackendError):
                    events.emit("backend_crash", traceback.format_exc(), level="error")
                info["error"] = str(e)
                report.errors.append(f"{b.id}: {e}")
                events.emit("backend_error", f"{b.id}: {e}", level="error")
                n = state.data["backend_errors"].get(b.id, 0) + 1
                state.data["backend_errors"][b.id] = n
                items.append(AttentionItem(
                    f"{b.id}:{Reason.BACKEND_ERROR.value}", "critical" if n >= 3 else "warning",
                    f"{b.id} could not be checked ({n} run{'s' if n != 1 else ''} in a row)", Reason.BACKEND_ERROR.value, str(e),
                    "Run 'smart-updater doctor', then look at the command log for this run.", backend=b.id))
            else:
                state.data["backend_errors"][b.id] = 0

        if mode == "plan":
            for d in decisions:
                if d.status == Status.WOULD_UPDATE and d.security and d.attention is None:
                    d.attention, d.reason_code = "warning", Reason.PLAN_ONLY
                    d.reason = "A security update is available and applies cleanly, but this was a plan-only run so it was not installed. " + d.reason
                    d.suggested_action = "Run 'smart-updater apply' (or schedule it) to install it."
        items += [decision_to_item(d, cfg) for d in decisions if d.attention]

        for b in backends:
            with contextlib.suppress(Exception):
                report.reboot += b.reboot_required()
        report.reboot += windows_reboot_pending()
        if report.reboot:
            items.append(AttentionItem(
                "system:reboot_required", "warning", "Reboot required", Reason.REBOOT_REQUIRED.value,
                "Installed updates only take effect after a reboot. Packages: " + ", ".join(report.reboot[:5]),
                "Reboot when convenient. Smart Updater never reboots by itself.", escalate_after_days=cfg.reboot_critical_days))

        finalize_items(items, state, cfg, [b.id for b in backends] + ["system"])
        for d in decisions:
            for it in items:
                if it.id == d.attention_id:
                    d.age_days, d.first_seen = it.age_days, it.first_seen
        report.decisions = decisions
        report.attention = items
        report.status = compute_status(items)
        report.finished = now_iso()
        report.duration_s = time.monotonic() - t0
        if baseline is not None:
            report.health = baseline.to_dict()

        write_reports(report, state, events.run_dir)
        if mode == "apply":  # plan-only runs must not mask a stopped apply schedule
            with contextlib.suppress(Exception):
                looksee.notify(cfg, report.to_dict(), events)
        state.data["last_run"] = {"run_id": run_id, "mode": mode, "status": report.status, "finished": report.finished}
        state.save()
        state.prune_runs(cfg.keep_runs)
        events.emit("run_end", f"status={report.status}", status=report.status, duration_s=round(report.duration_s, 1))
        return report


def rollback_run(cfg, run_id: str, *, state_dir: Optional[str] = None, verbose: bool = False, quiet: bool = False) -> List[str]:
    """Re-apply the previous versions recorded by an earlier run."""
    _need_privilege("Rolling back")
    root = Path(state_dir or cfg.state_dir or default_state_dir())
    state = State(root)
    src = root / "runs" / run_id
    files = sorted(src.glob("snapshots-*.json"))
    if not files:
        raise EngineError(f"run {run_id} has no snapshots (nothing was applied, or the run was pruned)")
    events = EventLog(root, new_run_id() + "-rollback", verbose, quiet)
    msgs: List[str] = []
    with contextlib.ExitStack() as stack:
        stack.callback(events.close)
        stack.enter_context(RunLock(state.lock_path))
        runner = Runner(events, events.run_dir, read_only=False)
        by_id = {b.id: b for b in detect(cfg, runner, events)}
        for f in files:
            for snap in reversed(json.loads(f.read_text(encoding="utf-8"))):
                b = by_id.get(snap["backend"])
                if not b:
                    msgs.append(f"{snap['backend']}: backend not available now, skipped")
                    continue
                res = b.rollback(snap)
                now = b.installed_versions(list(snap["versions"]))
                bad = [n for n, v in snap["versions"].items() if v and now.get(n) != v]
                if res.ok and not bad:
                    msgs.append(f"{snap['backend']}: restored {len(snap['versions'])} package(s) to the versions from {snap.get('time', '?')}")
                else:
                    msgs.append(f"{snap['backend']}: rollback incomplete ({res.error or 'versions not restored: ' + ', '.join(bad)})")
        for m in msgs:
            events.emit("rollback_run", m)
    return msgs
