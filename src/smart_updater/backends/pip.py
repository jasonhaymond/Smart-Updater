"""pip backend (opt-in, one instance per configured environment).

Only environments you list in the config are touched. It never defaults to the
system Python, because upgrading libraries that the OS itself depends on is
exactly how machines get broken.

Parsers are fixture-tested. Needs pip >= 23.0 for ``install --dry-run --report``.
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, List

from ..models import ApplyResult, SimResult, Update
from .base import Backend, BackendError

_PIP_ENV = {"PIP_NO_INPUT": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}


def norm_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_outdated(text: str, backend_id: str) -> List[Update]:
    try:
        data = json.loads(text or "[]")
    except ValueError as e:
        raise BackendError(f"could not parse 'pip list --outdated' output: {e}")
    return [
        Update(backend=backend_id, name=norm_name(p["name"]), display=p["name"], installed=p["version"], candidate=p["latest_version"])
        for p in data
    ]


def parse_report(text: str, installed: Dict[str, str]):
    """Turn a ``pip install --dry-run --report -`` JSON report into upgrades/installs."""
    report = json.loads(text)
    upgrades: Dict[str, str] = {}
    previous: Dict[str, str] = {}
    installs: Dict[str, str] = {}
    for item in report.get("install", []):
        meta = item.get("metadata", {})
        name, ver = norm_name(meta.get("name", "")), meta.get("version", "")
        if not name:
            continue
        if name in installed:
            if installed[name] != ver:
                upgrades[name] = ver
                previous[name] = installed[name]
        else:
            installs[name] = ver
    return upgrades, previous, installs


def parse_conflict_names(text: str) -> List[str]:
    names: List[str] = []
    for a, b in re.findall(r"^\s+(\S+) \S+ depends on (\S+)", text, re.M):
        names += [norm_name(a), norm_name(re.split(r"[<>=!~;\[ ]", b)[0])]
    m = re.search(r"Cannot install (\S+?)(?:,| and )(?: )?(\S+)?", text)
    if m:
        names += [norm_name(g) for g in m.groups() if g]
    return names


class PipBackend(Backend):
    NAME = "pip"
    scheme = "pep440"
    stable_release = False
    rollback_support = "best_effort"
    default_max_bump = "minor"
    osv_ecosystem = "PyPI"

    def __init__(self, runner, events, options, env_path: str):
        super().__init__(runner, events, options)
        self.env_path = sys.executable if env_path == "current" else env_path
        self.python = self._python_for(self.env_path)

    @staticmethod
    def _python_for(env: str) -> str:
        if os.path.isfile(env):
            return env
        sub = os.path.join("Scripts", "python.exe") if os.name == "nt" else os.path.join("bin", "python")
        return os.path.join(env, sub)

    @property
    def id(self) -> str:
        return f"pip@{self.env_path}"

    @classmethod
    def available(cls) -> bool:
        return True

    def norm(self, name: str) -> str:
        return norm_name(name)

    def _pip(self, *args: str) -> List[str]:
        return [self.python, "-m", "pip", "--disable-pip-version-check", *args]

    def list_updates(self) -> List[Update]:
        r = self.run(self._pip("list", "--outdated", "--format=json"), label="pip-outdated", timeout=300, env=_PIP_ENV)
        if not r.ok:
            raise BackendError(f"pip list --outdated failed in {self.env_path} (rc={r.rc}): {r.stderr.strip()[-300:]}")
        return parse_outdated(r.stdout, self.id)

    def installed_all(self) -> Dict[str, str]:
        r = self.run(self._pip("list", "--format=json"), label="pip-list", env=_PIP_ENV)
        if not r.ok:
            return {}
        try:
            return {norm_name(p["name"]): p["version"] for p in json.loads(r.stdout)}
        except ValueError:
            return {}

    def installed_versions(self, names: List[str]) -> Dict[str, str]:
        everything = self.installed_all()
        return {n: everything[n] for n in names if n in everything}

    def simulate(self, targets: List[Update]) -> SimResult:
        specs = [f"{t.name}=={t.candidate}" for t in targets]
        r = self.run(
            self._pip("install", "--dry-run", "--report", "-", "--quiet", *specs),
            label="pip-simulate",
            timeout=300,
            env=_PIP_ENV,
        )
        if not r.ok:
            err = r.stderr.strip()
            return SimResult(False, error=err[-700:], blockers=parse_conflict_names(err))
        try:
            upgrades, previous, installs = parse_report(r.stdout, self.installed_all())
        except ValueError as e:
            return SimResult(False, error=f"could not parse pip's dry-run report ({e}); pip >= 23.0 is required")
        return SimResult(True, upgrades=upgrades, previous=previous, installs=installs)

    def problems(self) -> List[str]:
        r = self.run(self._pip("check"), label="pip-check", env=_PIP_ENV)
        return [] if r.ok else [l.strip() for l in r.stdout.splitlines() if l.strip()]

    def apply(self, sim: SimResult) -> ApplyResult:
        specs = [f"{n}=={v}" for n, v in sim.upgrades.items()]
        r = self.run(self._pip("install", "--no-input", *specs), label="pip-apply", timeout=self.options.get("apply_timeout", 3600), env=_PIP_ENV, mutating=True)
        return ApplyResult(r.ok, r.rc, "" if r.ok else r.stderr.strip()[-500:], log_path=r.log_path)

    def rollback(self, snapshot: Dict[str, Any]) -> ApplyResult:
        specs = [f"{n}=={v}" for n, v in snapshot.get("versions", {}).items() if v]
        if not specs:
            return ApplyResult(True)
        r = self.run(self._pip("install", "--no-input", *specs), label="pip-rollback", timeout=self.options.get("apply_timeout", 3600), env=_PIP_ENV, mutating=True)
        return ApplyResult(r.ok, r.rc, "" if r.ok else r.stderr.strip()[-500:], log_path=r.log_path)
