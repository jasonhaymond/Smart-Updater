"""Reports: what a run found and decided, in a form people and monitors can use.

``latest.json`` (schema ``smart-updater/report/v1``) is the integration point.
``check_report`` turns it into a Nagios-style status line and exit code.
"""
from __future__ import annotations

import json
import platform
import socket
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from . import REPORT_SCHEMA, __version__
from .models import AttentionItem, Decision, Reason, Status
from .state import State, atomic_write_text

STATUS_CODE = {"ok": 0, "warning": 1, "critical": 2, "unknown": 3}
_SEV = {"info": 0, "warning": 1, "critical": 2}

SHORT = {
    Reason.HELD: "held back in the package manager",
    Reason.RULE_HOLD: "held by a rule",
    Reason.NEEDS_HUMAN: "needs a person to update it",
    Reason.BUMP_TOO_LARGE: "version jump needs approval",
    Reason.WOULD_REMOVE: "update would remove packages",
    Reason.DEPENDENCY_BLOCKED: "blocked by a dependency that can't change",
    Reason.CASCADE_LIMIT: "dependency chain too long to resolve safely",
    Reason.NEW_PACKAGES: "would install new packages",
    Reason.SIMULATION_FAILED: "dry run failed",
    Reason.APPLY_FAILED: "update failed to apply",
    Reason.VERIFY_FAILED: "update broke something and was rolled back",
    Reason.VERSION_MISMATCH: "update did not take effect",
    Reason.ROLLBACK_FAILED: "update failed and rollback did not fully work",
    Reason.QUARANTINED: "paused after a failed attempt",
    Reason.NO_FIX_AVAILABLE: "vulnerable and no fix is available",
    Reason.RUN_ABORTED: "not attempted (run aborted)",
    Reason.PLAN_ONLY: "security update available, not applied (plan-only run)",
    Reason.BACKEND_ERROR: "package manager could not be queried",
    Reason.REBOOT_REQUIRED: "reboot required",
    Reason.HEALTH_BASELINE: "health check was already failing",
}


def decision_to_item(d: Decision, cfg) -> AttentionItem:
    short = SHORT.get(d.reason_code, d.reason_code.value.replace("_", " "))
    return AttentionItem(
        id=d.attention_id,
        severity=d.attention or "info",
        title=f"{d.display or d.name} {d.installed} → {d.candidate}: {short}",
        reason_code=d.reason_code.value,
        reason=d.reason,
        suggested_action=d.suggested_action,
        backend=d.backend,
        package=d.name,
        installed=d.installed,
        candidate=d.candidate,
        security=d.security,
        advisories=list(d.advisories),
        escalate_after_days=cfg.critical_after_days if d.security and d.attention == "warning" else None,
    )


def finalize_items(items: List[AttentionItem], state: State, cfg, covered_prefixes: List[str], now: Optional[datetime] = None) -> None:
    """Attach first-seen/age, escalate stale security items, forget resolved ones."""
    now = now or datetime.now().astimezone()
    seen = state.data["first_seen"]
    current = set()
    for it in items:
        current.add(it.id)
        first = seen.get(it.id) or now.isoformat(timespec="seconds")
        seen[it.id] = first
        it.first_seen = first
        it.age_days = max(0.0, (now - datetime.fromisoformat(first)).total_seconds() / 86400)
        if it.severity == "warning" and it.escalate_after_days is not None and it.age_days >= it.escalate_after_days:
            it.severity = "critical"
            it.reason += f" This has been unresolved for {it.age_days:.1f} days, so it is now critical."
    for k in list(seen):
        if k not in current and any(k.startswith(p + ":") for p in covered_prefixes):
            del seen[k]


def compute_status(items: List[AttentionItem]) -> str:
    worst = max((_SEV[i.severity] for i in items), default=0)
    return "critical" if worst == 2 else "warning" if worst == 1 else "ok"


@dataclass
class RunReport:
    run_id: str
    mode: str
    started: str
    finished: str = ""
    duration_s: float = 0.0
    backends: List[Dict[str, Any]] = field(default_factory=list)
    decisions: List[Decision] = field(default_factory=list)
    attention: List[AttentionItem] = field(default_factory=list)
    reboot: List[str] = field(default_factory=list)
    health: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    status: str = "ok"

    @property
    def exit_code(self) -> int:
        return STATUS_CODE[self.status]

    def summary(self) -> Dict[str, Any]:
        counts = Counter(d.status.value for d in self.decisions)
        att = Counter(i.severity for i in self.attention)
        return {
            "updated": counts.get("updated", 0),
            "would_update": counts.get("would_update", 0),
            "blocked": counts.get("blocked", 0),
            "skipped": counts.get("skipped", 0),
            "rolled_back": counts.get("rolled_back", 0),
            "failed": counts.get("failed", 0),
            "security_pending": sum(1 for i in self.attention if i.security),
            "attention": {s: att.get(s, 0) for s in ("critical", "warning", "info")},
        }

    def to_dict(self) -> Dict[str, Any]:
        order = sorted(self.attention, key=lambda i: (-_SEV[i.severity], not i.security, i.title))
        return {
            "schema": REPORT_SCHEMA,
            "tool": {"name": "smart-updater", "version": __version__},
            "run_id": self.run_id,
            "mode": self.mode,
            "host": socket.gethostname(),
            "platform": f"{platform.system()} {platform.release()}",
            "started": self.started,
            "finished": self.finished,
            "duration_s": round(self.duration_s, 1),
            "status": self.status,
            "exit_code": self.exit_code,
            "summary": self.summary(),
            "reboot_required": {"required": bool(self.reboot), "reasons": self.reboot},
            "attention": [i.to_dict() for i in order],
            "decisions": [d.to_dict() for d in self.decisions],
            "backends": self.backends,
            "health": self.health,
            "errors": self.errors,
            "notes": self.notes,
        }


def render_text(report: RunReport) -> str:
    s = report.summary()
    out: List[str] = []
    out.append(f"Smart Updater report  run={report.run_id}  mode={report.mode}  host={socket.gethostname()}")
    out.append(f"Finished {report.finished} in {report.duration_s:.0f}s")
    out.append(f"STATUS: {report.status.upper()} (exit code {report.exit_code})")
    out.append(
        f"Updated {s['updated']} | would update {s['would_update']} | blocked {s['blocked']} | skipped {s['skipped']} "
        f"| rolled back {s['rolled_back']} | failed {s['failed']} | security items open {s['security_pending']}"
    )
    out.append("Backends: " + (", ".join(f"{b['id']}{' (experimental)' if b.get('experimental') else ''}" for b in report.backends) or "none detected"))
    for e in report.errors:
        out.append(f"ERROR: {e}")
    for n in report.notes:
        out.append(f"note: {n}")

    items = sorted(report.attention, key=lambda i: (-_SEV[i.severity], not i.security, i.title))
    needs = [i for i in items if i.severity in ("critical", "warning")]
    fyi = [i for i in items if i.severity == "info"]
    by_pkg = {(d.backend, d.name): d for d in report.decisions}

    def block(i: AttentionItem) -> None:
        sec = " [SECURITY]" if i.security else ""
        out.append(f"  [{i.severity.upper()}]{sec} {i.title}")
        out.append(f"      why:   {i.reason}")
        d = by_pkg.get((i.backend, i.package))
        if d and d.chain:
            for c in d.chain:
                out.append(f"      chain: {c}")
        if i.advisories:
            out.append(f"      advisories: {', '.join(i.advisories[:6])}{' ...' if len(i.advisories) > 6 else ''}")
        if i.suggested_action:
            out.append(f"      do:    {i.suggested_action}")
        if i.age_days >= 0.1:
            out.append(f"      open for {i.age_days:.1f} days (first seen {i.first_seen})")

    out.append("")
    out.append(f"NEEDS ATTENTION ({len(needs)})" if needs else "NEEDS ATTENTION: nothing")
    for i in needs:
        block(i)
    if fyi:
        out.append("")
        out.append(f"FOR YOUR INFORMATION ({len(fyi)}): not urgent, but a person decided or must decide")
        for i in fyi:
            block(i)

    for label, st in (("UPDATED", Status.UPDATED), ("WOULD UPDATE (plan only)", Status.WOULD_UPDATE)):
        ds = [d for d in report.decisions if d.status == st]
        if ds:
            out.append("")
            out.append(f"{label} ({len(ds)})")
            for d in sorted(ds, key=lambda d: (not d.security, d.name)):
                tag = " [security]" if d.security else ""
                out.append(f"  {d.display or d.name} {d.installed} → {d.candidate}{tag}")
                for c in d.chain:
                    out.append(f"      chain: {c}")
    quiet = [d for d in report.decisions if d.status in (Status.SKIPPED, Status.BLOCKED) and not d.attention]
    if quiet:
        out.append("")
        out.append(f"LEFT ALONE ({len(quiet)})")
        for d in quiet:
            out.append(f"  {d.display or d.name}: {d.reason_code.value}: {d.reason}")
    if report.reboot:
        out.append("")
        out.append("REBOOT REQUIRED: " + "; ".join(report.reboot[:5]))
    return "\n".join(out) + "\n"


def write_reports(report: RunReport, state: State, run_dir) -> None:
    data = json.dumps(report.to_dict(), indent=2)
    text = render_text(report)
    atomic_write_text(run_dir / "report.json", data)
    atomic_write_text(run_dir / "report.txt", text)
    atomic_write_text(state.latest_json, data)
    atomic_write_text(state.latest_txt, text)


# ---- monitoring side ----------------------------------------------------------

def check_report(data: Dict[str, Any], max_age_hours: float, now: Optional[datetime] = None) -> Tuple[int, str]:
    """Nagios-style (exit code, one-line status) from a latest.json dict."""
    now = now or datetime.now().astimezone()
    try:
        age_h = (now - datetime.fromisoformat(data["finished"])).total_seconds() / 3600
        status = data["status"]
        s = data["summary"]
        att = s["attention"]
    except (KeyError, ValueError, TypeError):
        return 3, "SMART_UPDATER UNKNOWN - latest report is unreadable or from an incompatible version"
    # The first value is the one Looksee (and most monitors) graph and put thresholds on, so it is the
    # number of things that need a person: warnings plus criticals.
    perf = (
        f"attention={att['critical'] + att['warning']} attention_critical={att['critical']} "
        f"attention_warning={att['warning']} attention_info={att['info']} security_open={s['security_pending']} "
        f"updated={s['updated']} failed={s['failed']} rolled_back={s['rolled_back']} age_hours={age_h:.1f}"
    )
    if age_h > max_age_hours:
        return 3, f"SMART_UPDATER UNKNOWN - last run was {age_h:.0f}h ago (limit {max_age_hours:g}h) | {perf}"
    mode = " (plan-only)" if data.get("mode") == "plan" else ""
    code = STATUS_CODE.get(status, 3)
    if status == "ok":
        return code, f"SMART_UPDATER OK{mode} - nothing needs attention, {s['updated']} updated last run | {perf}"
    items = [i for i in data.get("attention", []) if i["severity"] in ("critical", "warning")]
    titles = "; ".join(i["title"] for i in items[:3]) + (f"; +{len(items) - 3} more" if len(items) > 3 else "")
    sec = f" ({s['security_pending']} security)" if s["security_pending"] else ""
    return code, f"SMART_UPDATER {status.upper()}{mode} - {len(items)} need attention{sec}: {titles} | {perf}"


_SEVERITY_ORDER = {"critical": 0, "warning": 1}


def _ascii(text: str) -> str:
    return " ".join(str(text).replace("\u2192", "->").split())


def _clip(text: str, n: int) -> str:
    text = _ascii(text)
    return text if len(text) <= n else text[: n - 3].rstrip() + "..."


def check_details(data: Dict[str, Any], budget: int = 3800) -> List[str]:
    """The lines to print after ``check``'s status line: one block per thing that needs a person.

    Monitors that read a script's output (Looksee keeps the first 4 KB and shows it on the check's page)
    can then show *what* needs attention and what to do, not just how many. The result never exceeds
    ``budget`` characters; if there is more, the last line says how many were left out.
    """
    items = [i for i in data.get("attention", []) if i.get("severity") in _SEVERITY_ORDER]
    items.sort(key=lambda i: (_SEVERITY_ORDER[i["severity"]], not i.get("security"), -float(i.get("age_days") or 0), i.get("id", "")))
    infos = sum(1 for i in data.get("attention", []) if i.get("severity") == "info")
    out: List[str] = []
    used = 0
    shown = 0
    for it in items:
        age = float(it.get("age_days") or 0)
        head = f"[{it['severity'].upper()}]{' [SECURITY]' if it.get('security') else ''} {_clip(it.get('title', ''), 160)}"
        if age >= 1:
            head += f" (open {age:.0f}d)"
        block = [head, "  why:  " + _clip(it.get("reason", ""), 320)]
        if it.get("suggested_action"):
            block.append("  next: " + _clip(it["suggested_action"], 220))
        block.append("  id:   " + _clip(it.get("id", ""), 100))
        size = sum(len(l) + 1 for l in block)
        if used + size > budget - 80:  # keep room for the "left out" line
            break
        out += block
        used += size
        shown += 1
    left = len(items) - shown
    tail = []
    if left:
        tail.append(f"+{left} more not shown here; run 'smart-updater report' on the host for all of them")
    if infos:
        tail.append(f"{infos} informational item(s) (held or deliberately skipped) are in the full report")
    return out + tail
