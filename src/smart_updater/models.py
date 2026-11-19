"""Shared data types.

Note: never format these enums with f-strings or str(); use ``.value``. The
behaviour of ``str(Enum)`` for str-mixin enums differs between Python versions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Bump(str, Enum):
    PATCH = "patch"
    MINOR = "minor"
    MAJOR = "major"
    UNKNOWN = "unknown"


_RANK = {"patch": 0, "minor": 1, "major": 2}


def bump_rank(value: Any) -> int:
    """Rank a bump size; anything unrecognised ranks as the largest."""
    return _RANK.get(getattr(value, "value", value), 2)


class Status(str, Enum):
    UPDATED = "updated"
    WOULD_UPDATE = "would_update"  # plan mode: would be applied
    BLOCKED = "blocked"  # cannot be applied safely
    SKIPPED = "skipped"  # deliberately not touched (hold, quarantine, limits)
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


class Reason(str, Enum):
    OK = "ok"
    HELD = "held"
    RULE_HOLD = "rule_hold"
    NEEDS_HUMAN = "needs_human"
    BUMP_TOO_LARGE = "bump_too_large"
    WOULD_REMOVE = "would_remove"
    DEPENDENCY_BLOCKED = "dependency_blocked"
    CASCADE_LIMIT = "cascade_limit"
    NEW_PACKAGES = "new_packages"
    SIMULATION_FAILED = "simulation_failed"
    APPLY_FAILED = "apply_failed"
    VERIFY_FAILED = "verify_failed"
    VERSION_MISMATCH = "version_mismatch"
    ROLLBACK_FAILED = "rollback_failed"
    QUARANTINED = "quarantined"
    NO_FIX_AVAILABLE = "no_fix_available"
    SECURITY_ONLY = "security_only"
    LIMIT_REACHED = "limit_reached"
    RUN_ABORTED = "run_aborted"
    PLAN_ONLY = "plan_only"
    BACKEND_ERROR = "backend_error"
    REBOOT_REQUIRED = "reboot_required"
    HEALTH_BASELINE = "health_baseline"


REASON_HELP: Dict[Reason, str] = {
    Reason.OK: "Safe to apply: simulation showed no removals and every dependency change passed policy.",
    Reason.HELD: "The package manager has this package held or pinned, so it was left alone.",
    Reason.RULE_HOLD: "A configured rule says never to touch this package.",
    Reason.NEEDS_HUMAN: "A rule says this package needs a person to approve the update.",
    Reason.BUMP_TOO_LARGE: "The version jump is bigger than policy allows for unattended updates.",
    Reason.WOULD_REMOVE: "Applying it would remove installed packages; Smart Updater never removes packages.",
    Reason.DEPENDENCY_BLOCKED: "It needs another package to change, and that change is not allowed.",
    Reason.CASCADE_LIMIT: "Resolving the dependency chain took more steps or packages than the configured limit.",
    Reason.NEW_PACKAGES: "It would install new packages and policy forbids that.",
    Reason.SIMULATION_FAILED: "The package manager's dry run failed, so Smart Updater did not try it for real.",
    Reason.APPLY_FAILED: "The package manager returned an error while applying.",
    Reason.VERIFY_FAILED: "The update applied but a health check got worse, so it was rolled back.",
    Reason.VERSION_MISMATCH: "The package manager reported success but the version did not change.",
    Reason.ROLLBACK_FAILED: "A rollback was needed and did not fully succeed. Check the machine now.",
    Reason.QUARANTINED: "A previous attempt at this version failed; it is paused so it does not retry every run.",
    Reason.NO_FIX_AVAILABLE: "A known vulnerability exists and no fixed version is available.",
    Reason.SECURITY_ONLY: "Skipped because this run only applies security updates.",
    Reason.LIMIT_REACHED: "Deferred to a later run because of the per-run update limit.",
    Reason.RUN_ABORTED: "The run stopped early after repeated failures.",
    Reason.PLAN_ONLY: "Plan-only run: nothing was changed.",
    Reason.BACKEND_ERROR: "A package manager could not be queried.",
    Reason.REBOOT_REQUIRED: "The machine needs a reboot for installed updates to take effect.",
    Reason.HEALTH_BASELINE: "A health check was already failing before any update was applied.",
}


@dataclass
class Update:
    backend: str  # backend instance id, e.g. "apt" or "pip@/opt/app/venv"
    name: str
    installed: str
    candidate: str
    display: str = ""
    security: bool = False
    advisories: List[str] = field(default_factory=list)
    source: str = ""
    bump: Bump = Bump.UNKNOWN

    @property
    def key(self) -> str:
        return f"{self.backend}:{self.name}"

    @property
    def label(self) -> str:
        return self.display or self.name

    @property
    def backend_name(self) -> str:
        return self.backend.split("@", 1)[0]


@dataclass
class SimResult:
    ok: bool
    upgrades: Dict[str, str] = field(default_factory=dict)  # name -> new version
    previous: Dict[str, str] = field(default_factory=dict)  # name -> version before
    installs: Dict[str, str] = field(default_factory=dict)  # new dependencies
    removals: List[str] = field(default_factory=list)
    held_back: List[str] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)  # names implicated in a failure
    error: str = ""
    analyzed: bool = True  # False when the backend cannot dry-run (e.g. winget)


@dataclass
class ApplyResult:
    ok: bool
    rc: int = 0
    error: str = ""
    token: Optional[str] = None  # backend-specific rollback handle
    log_path: str = ""


@dataclass
class Decision:
    backend: str
    name: str
    display: str
    installed: str
    candidate: str
    security: bool
    advisories: List[str]
    bump: str
    status: Status
    reason_code: Reason
    reason: str
    chain: List[str] = field(default_factory=list)
    pulled_in: List[str] = field(default_factory=list)
    attention: Optional[str] = None  # None | info | warning | critical
    suggested_action: str = ""
    age_days: float = 0.0
    first_seen: str = ""

    @property
    def key(self) -> str:
        return f"{self.backend}:{self.name}"

    @property
    def attention_id(self) -> str:
        return f"{self.backend}:{self.name}:{self.reason_code.value}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "package": self.name,
            "display": self.display,
            "installed": self.installed,
            "candidate": self.candidate,
            "security": self.security,
            "advisories": list(self.advisories),
            "bump": self.bump,
            "status": self.status.value,
            "reason_code": self.reason_code.value,
            "reason": self.reason,
            "chain": list(self.chain),
            "pulled_in": list(self.pulled_in),
            "attention": self.attention,
            "suggested_action": self.suggested_action,
            "age_days": round(self.age_days, 2),
            "first_seen": self.first_seen,
        }


@dataclass
class AttentionItem:
    """Something a human should look at. This is what monitoring consumes."""

    id: str
    severity: str  # info | warning | critical
    title: str
    reason_code: str
    reason: str
    suggested_action: str = ""
    backend: str = ""
    package: str = ""
    installed: str = ""
    candidate: str = ""
    security: bool = False
    advisories: List[str] = field(default_factory=list)
    escalate_after_days: Optional[float] = None
    first_seen: str = ""
    age_days: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "severity": self.severity,
            "title": self.title,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "suggested_action": self.suggested_action,
            "backend": self.backend,
            "package": self.package,
            "installed": self.installed,
            "candidate": self.candidate,
            "security": self.security,
            "advisories": list(self.advisories),
            "first_seen": self.first_seen,
            "age_days": round(self.age_days, 2),
        }
