"""Command line interface."""
from __future__ import annotations

import argparse
import fnmatch
import json
import platform
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__, looksee
from .backends import ALL_NAMES, SYSTEM_BACKENDS
from .config import Config, ConfigError
from .engine import EngineError, rollback_run, run_once
from .models import REASON_HELP, Reason
from .report import check_details, check_report, render_text
from .state import LockError, State, candidate_state_dirs, default_state_dir, is_privileged


def _state_root(args, cfg) -> Path:
    return Path(args.state_dir or cfg.state_dir or default_state_dir())


def _report_root(args, cfg) -> Path:
    """Where to read reports from. Unless a directory was named, look in the system and the user
    location, so a monitoring agent running as a different user than the updater still finds them."""
    if args.state_dir or cfg.state_dir:
        return _state_root(args, cfg)
    for d in candidate_state_dirs():
        if (d / "latest.json").is_file():
            return d
    return _state_root(args, cfg)


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", help="path to a TOML or JSON config file")
    p.add_argument("--state-dir", help="where reports, logs and state live")
    p.add_argument("-v", "--verbose", action="store_true", help="log debug detail to the console")
    p.add_argument("-q", "--quiet", action="store_true", help="only log warnings and errors to the console")


def _run_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--backend", action="append", choices=ALL_NAMES, help="only use this backend (repeatable)")
    p.add_argument("--security-only", action="store_true", help="only consider security updates")
    p.add_argument("--json", action="store_true", help="print the JSON report instead of the text report")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="smart-updater",
        description="Plans, simulates, applies, verifies and rolls back updates, and explains every decision.",
    )
    ap.add_argument("--version", action="version", version=f"smart-updater {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="show what would be updated and why (changes nothing)")
    _common(p); _run_opts(p)
    p.add_argument("--refresh", action="store_true", help="refresh package lists first (needs privileges on most systems)")

    p = sub.add_parser("apply", help="apply the safe updates, verify each, roll back on trouble")
    _common(p); _run_opts(p)

    p = sub.add_parser("check", help="monitoring check: reads the latest report, Nagios-style output and exit code")
    _common(p)
    p.add_argument("--max-age-hours", type=float, default=36, help="report older than this is UNKNOWN (default 36)")
    p.add_argument("--details", action="store_true", help="after the status line, list what needs attention, why and what to do (for monitors that keep the output)")

    p = sub.add_parser("push", help="tell Looksee about the latest report (calls a Heartbeat check's URL)")
    _common(p)
    p.add_argument("--max-age-hours", type=float, default=36)
    p.add_argument("--url-file", help="file holding the Looksee push URL (or set SMART_UPDATER_LOOKSEE_URL, or [looksee] push_url_file)")

    p = sub.add_parser("report", help="print the latest report")
    _common(p)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("explain", help="show everything recorded about a package in the latest report")
    _common(p)
    p.add_argument("package", help="package name or pattern, e.g. openssl or 'lib*'")

    p = sub.add_parser("rollback", help="restore the versions recorded by an earlier apply run")
    _common(p)
    p.add_argument("run_id")

    p = sub.add_parser("unquarantine", help="clear the pause on a package whose update failed earlier")
    _common(p)
    p.add_argument("package", nargs="?", help="package name; omit to clear all")

    p = sub.add_parser("doctor", help="show what was detected on this machine")
    _common(p)
    return ap


def _load_cfg(args) -> Config:
    cfg = Config.load(args.config)
    if getattr(args, "security_only", False):
        cfg.security_only = True
    return cfg


def cmd_run(args, mode: str) -> int:
    cfg = _load_cfg(args)
    report = run_once(
        cfg, mode, only=args.backend, refresh=getattr(args, "refresh", False),
        verbose=args.verbose, quiet=args.quiet, state_dir=args.state_dir,
    )
    print(json.dumps(report.to_dict(), indent=2) if args.json else render_text(report), end="\n" if args.json else "")
    return report.exit_code


def cmd_check(args) -> int:
    cfg = Config.load(args.config)
    path = _report_root(args, cfg) / "latest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"SMART_UPDATER UNKNOWN - no readable report at {path}")
        return 3
    code, line = check_report(data, args.max_age_hours)
    print(line)
    if args.details and code != 3:
        for extra in check_details(data):
            print(extra)
    return code


def cmd_push(args) -> int:
    cfg = Config.load(args.config)
    path = _report_root(args, cfg) / "latest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"error: no readable report at {path}", file=sys.stderr)
        return 3
    try:
        url = looksee.load_url(cfg, args.url_file)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    if not url:
        print(f"error: no Looksee URL set. Use --url-file, [looksee] push_url_file, or {looksee.ENV_URL}.", file=sys.stderr)
        return 3
    payload = looksee.build_payload(data, args.max_age_hours)
    ok, detail = looksee.push(url, payload, float(cfg.looksee.get("timeout", 10)))
    host = looksee.urlsplit(url).netloc
    if not ok:
        print(f"error: could not tell Looksee at {host}: {detail}", file=sys.stderr)
        return 3
    print(f"told Looksee at {host}: status={payload['status']} value={payload.get('value', '-')} ({detail})")
    return 0


def cmd_report(args) -> int:
    cfg = Config.load(args.config)
    path = _report_root(args, cfg) / ("latest.json" if args.json else "latest.txt")
    try:
        print(path.read_text(encoding="utf-8"), end="")
    except OSError:
        print(f"error: no report at {path}; run 'smart-updater plan' first", file=sys.stderr)
        return 3
    return 0


def cmd_explain(args) -> int:
    cfg = Config.load(args.config)
    path = _report_root(args, cfg) / "latest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"error: no readable report at {path}", file=sys.stderr)
        return 3
    pat = args.package.lower()
    hits = [d for d in data["decisions"] if fnmatch.fnmatch(d["package"].lower(), pat) or fnmatch.fnmatch((d["display"] or "").lower(), pat)]
    if not hits:
        print(f"Nothing in run {data['run_id']} mentions '{args.package}' (it may be up to date).")
        return 0
    for d in hits:
        print(f"{d['display'] or d['package']}  [{d['backend']}]  {d['installed']} -> {d['candidate']}  ({d['bump']} jump{', SECURITY' if d['security'] else ''})")
        print(f"  status: {d['status']}   reason: {d['reason_code']} - {REASON_HELP.get(Reason(d['reason_code']), '')}")
        print(f"  detail: {d['reason']}")
        for c in d["chain"]:
            print(f"  chain:  {c}")
        if d["pulled_in"]:
            print(f"  also changes: {', '.join(d['pulled_in'])}")
        if d["advisories"]:
            print(f"  advisories: {', '.join(d['advisories'])}")
        if d["suggested_action"]:
            print(f"  next:   {d['suggested_action']}")
        print()
    return 0


def cmd_rollback(args) -> int:
    cfg = _load_cfg(args)
    for m in rollback_run(cfg, args.run_id, state_dir=args.state_dir, verbose=args.verbose, quiet=args.quiet):
        print(m)
    return 0


def cmd_unquarantine(args) -> int:
    cfg = Config.load(args.config)
    state = State(_state_root(args, cfg))
    q = state.data["quarantine"]
    drop = [k for k in q if not args.package or args.package.lower() in k.lower()]
    for k in drop:
        del q[k]
    state.save()
    print(f"cleared {len(drop)} paused update(s)")
    return 0


def cmd_doctor(args) -> int:
    cfg = Config.load(args.config)
    root = _state_root(args, cfg)
    try:
        import tomllib  # noqa: F401
        toml = "yes"
    except ImportError:
        toml = "no (use a JSON config)"
    print(f"smart-updater {__version__}  python {platform.python_version()}  {platform.system()} {platform.release()}")
    print(f"privileged: {is_privileged()}   state dir: {root}   TOML configs: {toml}")
    print(f"config: {cfg.source}")
    for cls in SYSTEM_BACKENDS:
        enabled = cfg.backend_enabled(cls.NAME, True)
        here = cls.available()
        note = " (experimental)" if cls.experimental else ""
        print(f"  {cls.NAME:14s} installed={'yes' if here else 'no':3s} enabled={'yes' if enabled else 'no':3s} rollback={cls.rollback_support}{note}")
    envs = cfg.backend_options("pip").get("environments", [])
    print(f"  pip            opt-in: {'enabled for ' + ', '.join(envs) if cfg.backend_enabled('pip', False) and envs else 'not configured'}")
    print(f"  osv.dev lookups: {'on (pip only)' if cfg.osv_enabled else 'off'}   rules: {len(cfg.rules)} custom + built-in   health checks: {len(cfg.health_checks)}")
    return 0


def _safe_streams() -> None:
    """Reports contain characters such as arrows. A Windows console or a pipe may use a legacy code page
    that cannot encode them, which would crash the command; replace what cannot be written instead."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: Optional[List[str]] = None) -> int:
    _safe_streams()
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "plan":
            return cmd_run(args, "plan")
        if args.cmd == "apply":
            return cmd_run(args, "apply")
        return {
            "check": cmd_check, "push": cmd_push, "report": cmd_report, "explain": cmd_explain, "rollback": cmd_rollback,
            "unquarantine": cmd_unquarantine, "doctor": cmd_doctor,
        }[args.cmd](args)
    except (ConfigError, EngineError, LockError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
