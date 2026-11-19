"""Configuration and policy.

Config files may be TOML (Python 3.11+) or JSON. Everything has a safe default,
so running with no config file is fine.
"""
from __future__ import annotations

import fnmatch
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


class ConfigError(ValueError):
    pass


@dataclass
class Rule:
    match: List[str]
    action: str = "allow"  # allow | manual | hold
    max_bump: Optional[str] = None  # patch | minor | major
    reason: str = ""
    backend: Optional[str] = None  # restrict to one backend name, e.g. "apt"


@dataclass
class HealthCheck:
    name: str
    command: List[str]
    timeout: float = 15
    retries: int = 2
    retry_delay: float = 3


# Built-in rules. These are deliberately few and each carries its reason, which
# is shown to the user whenever it blocks something. A user rule that matches
# first (e.g. action = "allow") overrides them.
DEFAULT_RULES: List[Rule] = [
    Rule(
        ["grub*", "shim*", "shim-signed*"],
        "manual",
        reason="Bootloader update. A failed update can leave the machine unable to boot, so it should happen when someone can reach the console.",
    ),
    Rule(
        ["nvidia*", "*-dkms"],
        "manual",
        reason="Kernel-module package. It is rebuilt against the installed kernel; a failed build can remove the driver or filesystem module until fixed.",
    ),
    Rule(
        ["docker-ce", "docker-ce-cli", "docker.io", "containerd.io", "Docker.DockerDesktop"],
        "manual",
        reason="Container runtime. Upgrading restarts the daemon and can interrupt running containers.",
    ),
    Rule(
        ["Python.Python.*"],
        "manual",
        reason="Interpreter upgrade. It can break virtual environments and tools installed against the old interpreter.",
        backend="winget",
    ),
    # macOS
    Rule(
        ["macOS*"],
        "manual",
        reason="A macOS update. These need a restart, cannot be uninstalled, and on Apple silicon need an administrator's "
        "credentials, so a person should choose the moment.",
        backend="softwareupdate",
    ),
    Rule(
        ["docker-desktop"],
        "manual",
        reason="Container runtime. Upgrading restarts the daemon and can interrupt running containers.",
        backend="brew",
    ),
    Rule(
        ["python", "python@*", "postgresql*", "mysql*", "mariadb*"],
        "allow",
        max_bump="patch",
        reason="Interpreters and database servers: a bigger jump can break virtual environments or need a data migration.",
        backend="brew",
    ),
]

_TOP_KEYS = {
    "general",
    "backends",
    "rules",
    "health_checks",
    "looksee",
}
_LOOKSEE_KEYS = {"push_url_file", "timeout"}
_GENERAL_KEYS = {
    "state_dir", "keep_runs", "critical_after_days", "reboot_critical_days", "max_cascade_depth",
    "max_group_size", "batch_size", "max_updates_per_run", "max_failures", "quarantine_days",
    "security_only", "allow_new_installs", "osv_enabled", "apply_timeout",
}
_RULE_KEYS = {"match", "action", "max_bump", "reason", "backend"}
_HEALTH_KEYS = {"name", "command", "timeout", "retries", "retry_delay"}
BUMPS = ("patch", "minor", "major")


@dataclass
class Config:
    state_dir: Optional[str] = None
    keep_runs: int = 30
    critical_after_days: float = 3
    reboot_critical_days: float = 14
    max_cascade_depth: int = 4
    max_group_size: int = 25
    batch_size: int = 1
    max_updates_per_run: int = 0  # 0 = unlimited
    max_failures: int = 3
    quarantine_days: float = 7
    security_only: bool = False
    allow_new_installs: bool = True
    osv_enabled: bool = True
    apply_timeout: int = 3600
    backends: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    rules: List[Rule] = field(default_factory=list)
    health_checks: List[HealthCheck] = field(default_factory=list)
    looksee: Dict[str, Any] = field(default_factory=dict)
    source: str = "(defaults)"

    # ---- lookups ---------------------------------------------------------
    def backend_options(self, name: str) -> Dict[str, Any]:
        return self.backends.get(name, {})

    def backend_enabled(self, name: str, default: bool = True) -> bool:
        return bool(self.backend_options(name).get("enabled", default))

    def max_bump_for(self, backend_name: str, default: str) -> str:
        return self.backend_options(backend_name).get("max_bump", default)

    def match_rule(self, name: str, display: str, backend_name: str) -> Optional[Rule]:
        """First matching rule wins: user rules, then built-in rules."""
        for rule in list(self.rules) + DEFAULT_RULES:
            if rule.backend and rule.backend != backend_name:
                continue
            for pat in rule.match:
                p = pat.lower()
                if fnmatch.fnmatchcase(name.lower(), p) or (display and fnmatch.fnmatchcase(display.lower(), p)):
                    return rule
        return None

    # ---- loading ---------------------------------------------------------
    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        candidates: List[str] = []
        if path:
            candidates = [path]
        else:
            env = os.environ.get("SMART_UPDATER_CONFIG")
            if env:
                candidates.append(env)
            candidates += ["smart-updater.toml", "smart-updater.json"]
            if os.name == "nt":
                base = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "SmartUpdater"
                candidates += [str(base / "config.toml"), str(base / "config.json")]
            elif sys.platform == "darwin":
                base = Path("/Library/Application Support/SmartUpdater")
                user = Path.home() / "Library" / "Application Support" / "SmartUpdater"
                candidates += [str(d / n) for d in (base, user) for n in ("config.toml", "config.json")]
            else:
                candidates += [
                    "/etc/smart-updater/config.toml", "/etc/smart-updater/config.json",
                    str(Path.home() / ".config" / "smart-updater" / "config.toml"),
                    str(Path.home() / ".config" / "smart-updater" / "config.json"),
                ]
        for c in candidates:
            if Path(c).is_file():
                cfg = cls.from_dict(_read_file(Path(c)))
                cfg.source = str(Path(c).resolve())
                return cfg
        if path:
            raise ConfigError(f"config file not found: {path}")
        return cls()

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Config":
        _only_keys("top level", raw, _TOP_KEYS)
        cfg = cls()
        general = raw.get("general", {})
        _only_keys("[general]", general, _GENERAL_KEYS)
        for k, v in general.items():
            setattr(cfg, k, v)
        if cfg.batch_size < 1:
            raise ConfigError("[general] batch_size must be >= 1")
        cfg.looksee = dict(raw.get("looksee", {}))
        _only_keys("[looksee]", cfg.looksee, _LOOKSEE_KEYS)
        cfg.backends = {k: dict(v) for k, v in raw.get("backends", {}).items()}
        for name, opts in cfg.backends.items():
            mb = opts.get("max_bump")
            if mb is not None and mb not in BUMPS:
                raise ConfigError(f"[backends.{name}] max_bump must be one of {BUMPS}")
        for i, r in enumerate(raw.get("rules", [])):
            _only_keys(f"rules[{i}]", r, _RULE_KEYS)
            match = r.get("match")
            if not match:
                raise ConfigError(f"rules[{i}] needs a 'match' pattern")
            action = r.get("action", "allow")
            if action not in ("allow", "manual", "hold"):
                raise ConfigError(f"rules[{i}] action must be allow, manual or hold")
            mb = r.get("max_bump")
            if mb is not None and mb not in BUMPS:
                raise ConfigError(f"rules[{i}] max_bump must be one of {BUMPS}")
            cfg.rules.append(
                Rule([match] if isinstance(match, str) else list(match), action, mb, r.get("reason", ""), r.get("backend"))
            )
        for i, h in enumerate(raw.get("health_checks", [])):
            _only_keys(f"health_checks[{i}]", h, _HEALTH_KEYS)
            cmd = h.get("command")
            if isinstance(cmd, str):
                import shlex

                cmd = shlex.split(cmd, posix=(os.name != "nt"))
            if not cmd or not h.get("name"):
                raise ConfigError(f"health_checks[{i}] needs 'name' and 'command'")
            cfg.health_checks.append(
                HealthCheck(h["name"], list(cmd), h.get("timeout", 15), h.get("retries", 2), h.get("retry_delay", 3))
            )
        return cfg


def _only_keys(where: str, section: Dict[str, Any], allowed) -> None:
    unknown = sorted(set(section) - set(allowed))
    if unknown:
        raise ConfigError(f"unknown key(s) in {where}: {', '.join(unknown)} (allowed: {', '.join(sorted(allowed))})")


def _read_file(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        try:
            return json.loads(text)
        except ValueError as e:
            raise ConfigError(f"{path}: invalid JSON: {e}")
    try:
        import tomllib
    except ImportError:
        raise ConfigError(f"{path}: TOML configs need Python 3.11+. Use a .json config or upgrade Python.")
    try:
        return tomllib.loads(text)
    except Exception as e:
        raise ConfigError(f"{path}: invalid TOML: {e}")
