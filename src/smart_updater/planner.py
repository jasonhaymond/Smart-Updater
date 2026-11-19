"""The planner: decides, for every available update, whether it is safe, and why.

The key idea is *cascade resolution*. For an update we ask the package manager
for a dry run. If the dry run says "this would remove package B" or "this
conflicts with B", we do not give up: if B itself has an update available, we
add B to the group and simulate again. We repeat until the dry run is clean
(nothing removed, every dependency change allowed by policy) or until we hit a
hard stop, and we record each step so the report can explain it.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .models import Bump, Decision, Reason, SimResult, Status, Update, bump_rank
from .version import classify_bump


@dataclass
class Verdict:
    status: Status
    reason: Reason
    text: str
    attention: Optional[str] = None
    action: str = ""


@dataclass
class Group:
    """A set of packages that must change together."""

    roots: List[Update]
    sim: SimResult
    chain: List[str]
    members: List[str]


@dataclass
class Resolution:
    ok: bool
    group: Optional[Group] = None
    reason: Reason = Reason.OK
    text: str = ""
    chain: List[str] = field(default_factory=list)
    action: str = ""


@dataclass
class PlanResult:
    groups: List[Group]
    decisions: Dict[str, Decision]


def _fmt(u: Update) -> str:
    return f"{u.label} {u.installed} → {u.candidate}"


class Planner:
    def __init__(self, cfg, backend, events, updates: List[Update], quarantine: Optional[Dict[str, float]] = None):
        self.cfg = cfg
        self.backend = backend
        self.events = events
        self.updates = updates
        self.by_name: Dict[str, Update] = {u.name: u for u in updates}
        self.held = backend.held()
        self.quarantine = quarantine or {}
        for u in updates:
            if u.bump == Bump.UNKNOWN and u.installed:
                u.bump = classify_bump(u.installed, u.candidate, backend.scheme)

    # ---- policy ----------------------------------------------------------
    def _effective_bump(self, u: Update) -> Bump:
        if u.bump != Bump.UNKNOWN:
            return u.bump
        # Unparseable version. A distro vets versions per release, so assume small;
        # for anything else assume the worst.
        return Bump.PATCH if self.backend.stable_release else Bump.MAJOR

    def precheck(self, u: Update) -> Optional[Verdict]:
        """Rules that depend only on the package itself. None means 'allowed'."""
        sec = u.security
        warn_if_sec = "warning" if sec else "info"
        if self.cfg.security_only and not sec:
            return Verdict(Status.SKIPPED, Reason.SECURITY_ONLY, "Not a security update and this run is security-only.")

        expiry = self.quarantine.get(f"{u.key}:{u.candidate}")
        if expiry and expiry > time.time():
            return Verdict(
                Status.SKIPPED,
                Reason.QUARANTINED,
                f"A previous attempt to install {u.label} {u.candidate} failed and was rolled back, so it is paused "
                "instead of failing again every run. A newer version resets this automatically.",
                "warning",
                f"Find out why it failed (see the previous run's log), then retry with: smart-updater unquarantine {u.name}",
            )

        if u.name in self.held or self.backend.norm(u.name) in self.held:
            hint = self.backend.hold_hint(u.name)
            return Verdict(
                Status.SKIPPED,
                Reason.HELD,
                f"{u.label} is held/pinned in the package manager, so it was left alone on purpose.",
                warn_if_sec,
                f"Release the hold when you are ready to update it: {hint}" if hint else "",
            )

        rule = self.cfg.match_rule(u.name, u.display, u.backend_name)
        if rule and rule.action == "hold":
            return Verdict(
                Status.SKIPPED,
                Reason.RULE_HOLD,
                rule.reason or f"A configured rule says never to update {u.label} automatically.",
                warn_if_sec,
                "Remove the rule from your config if you want it updated automatically.",
            )
        if rule and rule.action == "manual":
            return Verdict(
                Status.BLOCKED,
                Reason.NEEDS_HUMAN,
                rule.reason or f"A rule requires a person to approve updates to {u.label}.",
                "warning",
                f"Update it yourself when you can watch it. To allow unattended updates, add a rule: "
                f'[[rules]] match = "{u.name}" action = "allow"',
            )

        # Something only the backend knows (e.g. "this update needs a restart"). A rule the user wrote with
        # action = "allow" overrides it, the same way it overrides the built-in manual rules.
        user_allowed = bool(rule and rule.action == "allow" and any(rule is r for r in self.cfg.rules))
        why = "" if user_allowed else self.backend.manual_reason(u)
        if why:
            return Verdict(
                Status.BLOCKED,
                Reason.NEEDS_HUMAN,
                why,
                "warning",
                f"Install it yourself when you can watch it. To allow unattended updates, add a rule: "
                f'[[rules]] match = "{u.name}" action = "allow"',
            )

        ceiling = rule.max_bump if rule and rule.max_bump else self.cfg.max_bump_for(u.backend_name, self.backend.default_max_bump)
        eff = self._effective_bump(u)
        if bump_rank(eff) > bump_rank(ceiling):
            return Verdict(
                Status.BLOCKED,
                Reason.BUMP_TOO_LARGE,
                f"{_fmt(u)} is a {eff.value} version jump; policy allows up to {ceiling} without a person checking first.",
                warn_if_sec,
                f"Read the release notes and update manually, or allow it: "
                f'[[rules]] match = "{u.name}" max_bump = "{eff.value}"',
            )
        return None

    # ---- cascade resolution ----------------------------------------------
    def _culprits(self, sim: SimResult, group: Dict[str, Update]) -> List[Update]:
        """Packages named in a failed dry run that have updates of their own."""
        names = set()
        for b in sim.blockers:
            names.update((b, b.split(":")[0]))
        for tok in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+:@\-]*", sim.error or ""):
            names.update((tok, tok.split(":")[0]))
        found: Dict[str, Update] = {}
        for n in names:
            u = self.by_name.get(self.backend.norm(n)) or self.by_name.get(n)
            if u and u.name not in group:
                found[u.name] = u
        return [found[k] for k in sorted(found)]

    def _first_blocked(self, candidates: List[Update]):
        for c in candidates:
            v = self.precheck(c)
            if v:
                return c, v
        return None

    def resolve(self, roots: List[Update]) -> Resolution:
        group: Dict[str, Update] = {u.name: u for u in roots}
        chain: List[str] = []
        for rnd in range(1, self.cfg.max_cascade_depth + 1):
            sim = self.backend.simulate(list(group.values()))
            self.events.emit(
                "simulate",
                f"round {rnd}: {', '.join(group)}",
                level="debug",
                ok=sim.ok,
                removals=sim.removals,
                upgrades=sim.upgrades,
                installs=sim.installs,
                error=sim.error[:300],
            )
            if not sim.ok:
                culprits = self._culprits(sim, group)
                if not culprits:
                    return Resolution(
                        False,
                        reason=Reason.SIMULATION_FAILED,
                        text=f"The package manager's dry run failed and nothing it names can be upgraded to fix it: {sim.error[:400]}",
                        chain=chain,
                        action="Run the package manager's own upgrade by hand to see the full message.",
                    )
                bad = self._first_blocked(culprits)
                if bad:
                    c, v = bad
                    return Resolution(
                        False,
                        reason=Reason.DEPENDENCY_BLOCKED,
                        text=f"Dry run failed because of {c.label}; upgrading it would fix that, but it is not allowed: {v.text}",
                        chain=chain,
                        action=v.action,
                    )
                if len(group) + len(culprits) > self.cfg.max_group_size:
                    return Resolution(False, reason=Reason.CASCADE_LIMIT, text="The dependency chain grew past the group-size limit.", chain=chain)
                chain.append(
                    f"Round {rnd}: dry run failed ({sim.error[:160]}). Also upgrading {', '.join(c.label for c in culprits)}, "
                    "which the failure names and which have updates available."
                )
                group.update({c.name: c for c in culprits})
                continue

            if sim.removals:
                fixers = [self.by_name[n] for n in sim.removals if n in self.by_name and n not in group]
                stuck = [n for n in sim.removals if n not in self.by_name or n in group]
                if stuck:
                    return Resolution(
                        False,
                        reason=Reason.WOULD_REMOVE,
                        text=f"Applying this would remove {', '.join(stuck)} and there is no newer version of it to move to. "
                        "Smart Updater never removes packages by itself.",
                        chain=chain,
                        action="Review the removal and run the upgrade yourself if it is what you want, or hold this package.",
                    )
                bad = self._first_blocked(fixers)
                if bad:
                    c, v = bad
                    return Resolution(
                        False,
                        reason=Reason.DEPENDENCY_BLOCKED,
                        text=f"It would replace {c.label}, which has a newer version, but updating that is not allowed: {v.text}",
                        chain=chain,
                        action=v.action,
                    )
                if len(group) + len(fixers) > self.cfg.max_group_size:
                    return Resolution(False, reason=Reason.CASCADE_LIMIT, text="The dependency chain grew past the group-size limit.", chain=chain)
                chain.append(
                    f"Round {rnd}: the dry run would remove {', '.join(f.name for f in fixers)}. Each has a newer version "
                    "available, so upgrading them together instead of removing them."
                )
                group.update({f.name: f for f in fixers})
                continue

            # Clean dry run: no failure, no removals. Check what else it changes.
            extras = [n for n in sim.upgrades if n not in group]
            for n in extras:
                u = self.by_name.get(n)
                v = self.precheck(u) if u else None
                if v:
                    return Resolution(
                        False,
                        reason=Reason.DEPENDENCY_BLOCKED,
                        text=f"It needs {u.label} to change as well, but that is not allowed: {v.text}",
                        chain=chain,
                        action=v.action,
                    )
            if sim.installs and not self.cfg.allow_new_installs:
                return Resolution(
                    False,
                    reason=Reason.NEW_PACKAGES,
                    text=f"It would install new packages ({', '.join(sim.installs)}) and policy forbids that.",
                    chain=chain,
                )
            if len(sim.upgrades) > self.cfg.max_group_size:
                return Resolution(False, reason=Reason.CASCADE_LIMIT, text="The change touches more packages than the group-size limit.", chain=chain)
            for n in extras:
                chain.append(f"Also upgrades {n} {sim.previous.get(n, '?')} → {sim.upgrades[n]} (required dependency, allowed by policy).")
            for n, v in sim.installs.items():
                chain.append(f"Installs new dependency {n} {v}.")
            if not sim.analyzed:
                chain.append("This package manager has no dry run, so dependencies could not be checked in advance.")
            members = sorted(set(group) | set(sim.upgrades))
            return Resolution(True, group=Group(list(group.values()), sim, chain, members), chain=chain)

        return Resolution(
            False,
            reason=Reason.CASCADE_LIMIT,
            text=f"Still not clean after {self.cfg.max_cascade_depth} rounds of pulling in dependencies.",
            chain=chain,
            action="Update it manually, or raise max_cascade_depth if you trust longer chains.",
        )

    # ---- decisions --------------------------------------------------------
    def _decision(self, u: Update, status: Status, reason: Reason, text: str, **kw) -> Decision:
        return Decision(
            backend=u.backend, name=u.name, display=u.label, installed=u.installed, candidate=u.candidate,
            security=u.security, advisories=list(u.advisories), bump=u.bump.value,
            status=status, reason_code=reason, reason=text, **kw,
        )

    def _from_verdict(self, u: Update, v: Verdict) -> Decision:
        return self._decision(u, v.status, v.reason, v.text, attention=v.attention, suggested_action=v.action)

    def _would_update(self, m: Update, g: Group, is_root: bool) -> Decision:
        if is_root:
            others = [n for n in g.members if n != m.name]
            text = f"Update available ({m.bump.value} jump). "
            if g.sim.analyzed:
                text += f"Dry run is clean: nothing would be removed; {len(g.members)} package(s) change."
            else:
                text += "No dry run is possible for this package manager; relying on policy, verification and rollback."
            return self._decision(m, Status.WOULD_UPDATE, Reason.OK, text, chain=list(g.chain), pulled_in=others + list(g.sim.installs))
        roots = ", ".join(r.label for r in g.roots)
        return self._decision(m, Status.WOULD_UPDATE, Reason.OK, f"Upgraded together with {roots} because it is a required dependency.")

    def plan(self) -> PlanResult:
        decisions: Dict[str, Decision] = {}
        groups: List[Group] = []
        covered = set()
        for u in sorted(self.updates, key=lambda x: (not x.security, x.name)):
            if u.name in covered:
                continue
            v = self.precheck(u)
            if v:
                decisions[u.key] = self._from_verdict(u, v)
                self.events.emit("decision", f"{_fmt(u)}: {v.status.value} ({v.reason.value})", level="info", package=u.name, reason=v.reason.value)
                continue
            res = self.resolve([u])
            if not res.ok:
                decisions[u.key] = self._decision(
                    u, Status.BLOCKED, res.reason, res.text, chain=list(res.chain),
                    attention="warning" if u.security else "info", suggested_action=res.action,
                )
                self.events.emit("decision", f"{_fmt(u)}: blocked ({res.reason.value}) {res.text[:200]}", level="info", package=u.name, reason=res.reason.value)
                continue
            groups.append(res.group)
            for name in res.group.members:
                covered.add(name)
                m = self.by_name.get(name)
                if m is not None:
                    decisions[m.key] = self._would_update(m, res.group, m.name == u.name)
            self.events.emit(
                "decision", f"{_fmt(u)}: safe to apply ({len(res.group.members)} package(s))", level="info",
                package=u.name, members=res.group.members,
            )
        limit = self.cfg.max_updates_per_run
        if limit and len(groups) > limit:
            for g in groups[limit:]:
                for name in g.members:
                    d = decisions.get(f"{self.backend.id}:{name}")
                    if d:
                        d.status, d.reason_code = Status.SKIPPED, Reason.LIMIT_REACHED
                        d.reason = f"Deferred to a later run (limit of {limit} update groups per run)."
            groups = groups[:limit]
        return PlanResult(groups, decisions)
