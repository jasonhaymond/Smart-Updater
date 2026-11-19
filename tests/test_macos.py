"""macOS support: Homebrew and softwareupdate parsers and behaviour, paths, rules and the launchd job.

None of this runs on a Mac. The backends are driven by a fake command runner that returns the output
format the real tools document, so these tests pin the parsing and the safety behaviour, not Homebrew.
"""
import json
import plistlib
import sys
from pathlib import Path

import pytest

from smart_updater import state
from smart_updater.backends import ALL_NAMES, BackendError, brew, softwareupdate
from smart_updater.backends.brew import BrewBackend
from smart_updater.backends.softwareupdate import SoftwareUpdateBackend
from smart_updater.config import Config
from smart_updater.models import Reason, SimResult, Status, Update
from smart_updater.planner import Planner
from smart_updater.runner import CmdResult, ReadOnlyViolation, Runner

ROOT = Path(__file__).resolve().parent.parent


class NullEvents:
    def emit(self, *a, **k):
        pass


class FakeRunner:
    """Answers commands from a list of (substring, CmdResult) pairs and remembers what was asked."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def run(self, cmd, *, label="", timeout=300, env=None, mutating=False):
        cmd = [str(c) for c in cmd]
        self.calls.append({"cmd": cmd, "label": label, "env": env, "mutating": mutating})
        line = " ".join(cmd)
        for needle, result in self.answers:
            if needle in line:
                return result if isinstance(result, CmdResult) else CmdResult(cmd, 0, stdout=result)
        return CmdResult(cmd, 0, stdout="")


def ok(out="", err=""):
    return CmdResult([], 0, stdout=out, stderr=err)


def fail(rc=1, out="", err=""):
    return CmdResult([], rc, stdout=out, stderr=err)


# ------------------------------------------------------------ Homebrew fixtures ----------

OUTDATED = json.dumps({
    "formulae": [
        {"name": "wget", "installed_versions": ["1.21.3"], "current_version": "1.21.4", "pinned": False, "pinned_version": None},
        {"name": "openssl@3", "installed_versions": ["3.2.0", "3.2.1"], "current_version": "3.3.0", "pinned": False},
        {"name": "node", "installed_versions": ["21.5.0"], "current_version": "22.0.0", "pinned": True, "pinned_version": "21.5.0"},
        {"name": "docker", "installed_versions": ["24.0.7"], "current_version": "25.0.0", "pinned": False},
    ],
    "casks": [
        {"name": "firefox", "installed_versions": "120.0", "current_version": "121.0"},
        {"name": "docker", "installed_versions": ["4.25.0"], "current_version": "4.26.0"},
    ],
})

INSTALLED = json.dumps({
    "formulae": [
        {"name": "wget", "installed": [{"version": "1.21.3"}, {"version": "1.21.4"}], "linked_keg": "1.21.4"},
        {"name": "jq", "installed": [{"version": "1.7.1"}], "linked_keg": None},
        {"name": "docker", "installed": [{"version": "24.0.7"}], "linked_keg": "24.0.7"},
    ],
    "casks": [
        {"token": "firefox", "installed": "121.0"},
        {"token": "docker", "installed": "4.26.0"},
        {"token": "unused", "installed": None},
    ],
})

DRY_RUN = """==> Would upgrade 2 outdated packages:
wget 1.21.3 -> 1.21.4
homebrew/core/libidn2 2.3.4, 2.3.5 -> 2.3.7
"""


def test_brew_outdated_parse_names_pins_and_collisions():
    updates, pinned = brew.parse_outdated(OUTDATED)
    by = {u.name: u for u in updates}
    assert by["wget"].installed == "1.21.3" and by["wget"].candidate == "1.21.4" and by["wget"].source == "formula"
    assert by["openssl@3"].installed == "3.2.1"  # several kegs installed: the newest is the current one
    assert by["firefox"].source == "cask" and by["firefox"].installed == "120.0"
    assert pinned == {"node"}
    # a formula and a cask with the same name must stay distinguishable
    assert by["docker"].source == "formula" and by["cask:docker"].source == "cask"
    assert by["cask:docker"].display == "docker"


def test_brew_installed_parse_uses_linked_version_and_same_naming():
    got = brew.parse_installed(INSTALLED)
    assert got["wget"] == "1.21.4"  # linked keg wins over the older kept one
    assert got["jq"] == "1.7.1"
    assert got["docker"] == "24.0.7" and got["cask:docker"] == "4.26.0"
    assert "unused" not in got


def test_brew_dry_run_parse_includes_dependents_and_strips_taps():
    got = brew.parse_dry_run(DRY_RUN)
    assert got == {"wget": ("1.21.3", "1.21.4"), "libidn2": ("2.3.5", "2.3.7")}
    assert brew.parse_dry_run("==> Would install 1 package:\nfoo 1.0 -> 2.0\n") == {}  # other sections are ignored
    assert brew.parse_dry_run("Error: nothing") == {}


def test_brew_bad_json_is_a_backend_error_not_a_crash():
    with pytest.raises(BackendError):
        brew.parse_outdated("Warning: not json")


def make_brew(answers, monkeypatch, root=False, owner="jason", options=None):
    runner = FakeRunner(answers)
    b = BrewBackend(runner, NullEvents(), options or {"apply_timeout": 60})
    b.brew = "/opt/homebrew/bin/brew"
    monkeypatch.setattr("smart_updater.backends.brew.os.geteuid", lambda: 0 if root else 501)
    monkeypatch.setattr(BrewBackend, "_owner", lambda self: owner)
    return b, runner


def test_brew_runs_as_the_homebrew_owner_when_root(monkeypatch):
    b, runner = make_brew([("outdated", OUTDATED)], monkeypatch, root=True)
    b.list_updates()
    cmd = runner.calls[0]["cmd"]
    assert cmd[:5] == ["sudo", "-n", "-u", "jason", "-H"]  # never brew as root
    assert "/opt/homebrew/bin/brew" in cmd and "outdated" in cmd
    assert "HOMEBREW_NO_INSTALL_CLEANUP=1" in cmd  # keep the previous version on disk
    assert "HOMEBREW_NO_AUTO_UPDATE=1" in cmd


def test_brew_does_not_use_sudo_when_already_the_owner(monkeypatch):
    b, runner = make_brew([("outdated", OUTDATED)], monkeypatch, root=False)
    b.list_updates()
    assert runner.calls[0]["cmd"][0] == "/opt/homebrew/bin/brew"
    assert runner.calls[0]["env"]["HOMEBREW_NO_INSTALL_CLEANUP"] == "1"


def test_brew_owned_by_root_is_refused_with_a_clear_reason(monkeypatch):
    b, _ = make_brew([], monkeypatch, root=True, owner="")
    with pytest.raises(BackendError, match="will not run as root"):
        b.list_updates()


def test_brew_greedy_casks_option_is_passed_through(monkeypatch):
    b, runner = make_brew([("outdated", OUTDATED)], monkeypatch, options={"greedy_casks": True})
    b.list_updates()
    assert "--greedy" in runner.calls[0]["cmd"]


def test_brew_failed_listing_says_why(monkeypatch):
    b, _ = make_brew([("outdated", fail(1, err="Error: broken"))], monkeypatch)
    with pytest.raises(BackendError, match="broken"):
        b.list_updates()


def test_brew_held_includes_pinned_from_both_sources(monkeypatch):
    b, _ = make_brew([("outdated", OUTDATED), ("--pinned", "wget\n")], monkeypatch)
    b.list_updates()
    assert b.held() == {"wget", "node"}


def test_brew_simulate_reports_dependents_as_extras(monkeypatch):
    b, runner = make_brew([("outdated", OUTDATED), ("--dry-run", DRY_RUN)], monkeypatch)
    ups = {u.name: u for u in b.list_updates()}
    sim = b.simulate([ups["wget"]])
    assert sim.ok and sim.analyzed
    assert sim.upgrades == {"wget": "1.21.4", "libidn2": "2.3.7"}
    assert sim.previous["libidn2"] == "2.3.5"
    assert not sim.removals
    dry = [c for c in runner.calls if "--dry-run" in c["cmd"]][0]
    assert "--formula" in dry["cmd"] and not dry["mutating"]


def test_brew_simulate_sends_casks_separately_and_unprefixes_collisions(monkeypatch):
    b, runner = make_brew([("outdated", OUTDATED), ("--dry-run", "==> Would upgrade 1 outdated package:\ndocker 4.25.0 -> 4.26.0\n")], monkeypatch)
    ups = {u.name: u for u in b.list_updates()}
    sim = b.simulate([ups["cask:docker"]])
    dry = [c for c in runner.calls if "--dry-run" in c["cmd"]][0]["cmd"]
    assert "--cask" in dry and "docker" in dry and "cask:docker" not in dry
    assert sim.upgrades == {"cask:docker": "4.26.0"}


def test_brew_without_dry_run_support_falls_back_to_unanalysed(monkeypatch):
    b, _ = make_brew([("outdated", OUTDATED), ("--dry-run", fail(1, err="Error: invalid option: --dry-run"))], monkeypatch)
    ups = {u.name: u for u in b.list_updates()}
    sim = b.simulate([ups["wget"]])
    assert sim.ok and not sim.analyzed and sim.upgrades == {"wget": "1.21.4"}


def test_brew_dry_run_failure_is_a_failed_simulation(monkeypatch):
    b, _ = make_brew([("outdated", OUTDATED), ("--dry-run", fail(1, err="Error: wget: no such formula"))], monkeypatch)
    ups = {u.name: u for u in b.list_updates()}
    sim = b.simulate([ups["wget"]])
    assert not sim.ok and "no such formula" in sim.error


def test_brew_apply_is_marked_mutating_and_never_cleans_up(monkeypatch):
    b, runner = make_brew([("outdated", OUTDATED)], monkeypatch, root=True)
    b.list_updates()
    res = b.apply(SimResult(True, upgrades={"wget": "1.21.4", "firefox": "121.0"}))
    assert res.ok
    applies = [c for c in runner.calls if c["label"].startswith("brew-apply")]
    assert len(applies) == 2 and all(c["mutating"] for c in applies)
    flat = [" ".join(c["cmd"]) for c in applies]
    assert any("upgrade --formula wget" in f for f in flat) and any("upgrade --cask firefox" in f for f in flat)
    assert not any("cleanup" in " ".join(c["cmd"]) for c in runner.calls)


def test_brew_apply_failure_carries_the_output(monkeypatch):
    b, _ = make_brew([("upgrade --formula", fail(1, err="Error: sudo: a password is required"))], monkeypatch)
    res = b.apply(SimResult(True, upgrades={"wget": "1.21.4"}))
    assert not res.ok and "password is required" in res.error


def test_brew_apply_is_blocked_in_read_only_mode(monkeypatch):
    # The real Runner, in plan (read-only) mode, must refuse before anything is executed.
    monkeypatch.setattr("smart_updater.backends.brew.os.geteuid", lambda: 501)
    b = BrewBackend(Runner(NullEvents(), None, read_only=True), NullEvents(), {})
    b.brew = "/nonexistent/brew"
    with pytest.raises(ReadOnlyViolation):
        b.apply(SimResult(True, upgrades={"wget": "1.21.4"}))


def test_brew_rollback_is_a_noop_when_nothing_changed_and_honest_when_it_did(monkeypatch):
    b, _ = make_brew([("info", INSTALLED)], monkeypatch)
    untouched = b.rollback({"versions": {"wget": "1.21.4"}})
    assert untouched.ok
    changed = b.rollback({"versions": {"wget": "1.21.3"}})
    assert not changed.ok
    assert "wget 1.21.3 -> 1.21.4" in changed.error and "brew cleanup" in changed.error


def test_brew_problems_reads_brew_missing(monkeypatch):
    b, _ = make_brew([("missing", fail(1, out="foo: bar baz\n"))], monkeypatch)
    assert b.problems() == ["foo: bar baz"]
    b, _ = make_brew([("missing", ok())], monkeypatch)
    assert b.problems() == []


def test_brew_is_only_available_on_a_mac(monkeypatch):
    monkeypatch.setattr(brew, "find_brew", lambda: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(sys, "platform", "linux")
    assert not BrewBackend.available()
    monkeypatch.setattr(sys, "platform", "darwin")
    assert BrewBackend.available()


def test_brew_does_not_trust_path_when_root(monkeypatch):
    monkeypatch.setattr(brew.os.path, "isfile", lambda p: False)
    monkeypatch.setattr(brew.os, "geteuid", lambda: 0)
    monkeypatch.setattr(brew.shutil, "which", lambda n: "/Users/someone/bin/brew")
    assert brew.find_brew() is None


# ------------------------------------------------------- softwareupdate fixtures ----------

SU_LIST = """Software Update Tool

Finding available software
Software Update found the following new or updated software:
* Label: Command Line Tools for Xcode-15.1
\tTitle: Command Line Tools for Xcode, Version: 15.1, Size: 706019KiB, Recommended: YES,
* Label: Safari17.2.1MojaveAuto-17.2.1
\tTitle: Safari, Version: 17.2.1, Size: 120001KiB, Recommended: YES,
* Label: macOS Sonoma 14.2.1-23C71
\tTitle: macOS Sonoma 14.2.1, Version: 14.2.1, Size: 3158090KiB, Recommended: YES, Action: restart,
"""

SU_NONE = "Software Update Tool\n\nFinding available software\nNo new software available.\n"


def test_softwareupdate_parse_titles_versions_and_restart():
    ups = softwareupdate.parse_list(SU_LIST)
    by = {u.name: u for u in ups}
    assert by["Command Line Tools for Xcode-15.1"].display == "Command Line Tools for Xcode"  # a comma inside the title is fine
    assert by["Command Line Tools for Xcode-15.1"].candidate == "15.1" and by["Command Line Tools for Xcode-15.1"].source == ""
    assert by["macOS Sonoma 14.2.1-23C71"].source == "restart"
    assert len(ups) == 3


def test_softwareupdate_nothing_available_and_unreadable_output():
    assert softwareupdate.parse_list(SU_NONE) == []
    assert softwareupdate.parse_list("") == []
    with pytest.raises(BackendError):
        softwareupdate.parse_list("Software Update found the following new or updated software:\nsomething else entirely\n")


def make_su(answers):
    runner = FakeRunner(answers)
    return SoftwareUpdateBackend(runner, NullEvents(), {"apply_timeout": 60}), runner


def plan_for(backend, cfg=None):
    cfg = cfg or Config()
    ups = backend.list_updates()
    return Planner(cfg, backend, NullEvents(), ups).plan()


def test_softwareupdate_applies_small_updates_and_leaves_macos_and_restarts_to_a_person():
    b, _ = make_su([("--list", ok(err=SU_LIST))])  # the real tool writes to stderr
    plan = plan_for(b)
    st = {k.split(":", 1)[1]: d for k, d in plan.decisions.items()}
    assert st["Command Line Tools for Xcode-15.1"].status == Status.WOULD_UPDATE
    assert st["Safari17.2.1MojaveAuto-17.2.1"].status == Status.WOULD_UPDATE
    macos = st["macOS Sonoma 14.2.1-23C71"]
    assert macos.status == Status.BLOCKED and macos.reason_code == Reason.NEEDS_HUMAN and macos.attention == "warning"
    assert "restart" in macos.reason.lower()


def test_softwareupdate_restart_flag_blocks_even_without_a_macos_name():
    text = SU_LIST.replace("macOS Sonoma 14.2.1-23C71", "Security Update 2024-001").replace("macOS Sonoma 14.2.1,", "Security Update 2024-001,")
    b, _ = make_su([("--list", ok(err=text))])
    d = plan_for(b).decisions["softwareupdate:Security Update 2024-001"]
    assert d.status == Status.BLOCKED and "restart" in d.reason.lower() and "never restarts" in d.reason


def test_user_allow_rule_overrides_the_backend_restart_objection():
    cfg = Config.from_dict({"rules": [{"match": "Security Update*", "action": "allow", "backend": "softwareupdate"}]})
    text = SU_LIST.replace("macOS Sonoma 14.2.1-23C71", "Security Update 2024-001")
    b, _ = make_su([("--list", ok(err=text))])
    assert plan_for(b, cfg).decisions["softwareupdate:Security Update 2024-001"].status == Status.WOULD_UPDATE


def test_softwareupdate_apply_never_restarts_and_verifies_it_is_gone():
    b, runner = make_su([("--list", ok(err=SU_LIST))])
    b.list_updates()
    label = "Command Line Tools for Xcode-15.1"
    res = b.apply(SimResult(True, upgrades={label: "15.1"}))
    installs = [c for c in runner.calls if "--install" in c["cmd"]]
    assert installs and installs[0]["mutating"]
    assert "--restart" not in installs[0]["cmd"] and "-R" not in installs[0]["cmd"]
    assert installs[0]["cmd"][-3:] == ["--install", label, "--agree-to-license"]
    # the fake still lists the label afterwards, so the "success" must not be believed
    assert not res.ok and "still offered" in res.error


def test_softwareupdate_apply_succeeds_when_the_label_disappears():
    b, runner = make_su([])
    answers = iter([ok(err=SU_LIST), ok(), ok(err=SU_NONE)])  # list, install, list again
    runner.run = lambda cmd, **k: next(answers)
    b.list_updates()
    res = b.apply(SimResult(True, upgrades={"Command Line Tools for Xcode-15.1": "15.1"}))
    assert res.ok


def test_softwareupdate_rollback_is_honest():
    b, _ = make_su([("--list", ok(err=SU_LIST))])
    assert b.rollback({"members": ["Command Line Tools for Xcode-15.1"]}).ok  # still pending: nothing to undo
    b, _ = make_su([("--list", ok(err=SU_NONE))])
    res = b.rollback({"members": ["Command Line Tools for Xcode-15.1"]})
    assert not res.ok and "cannot be uninstalled" in res.error


def test_softwareupdate_is_only_available_on_a_mac(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert not SoftwareUpdateBackend.available()


# ---------------------------------------------------------------- paths, rules, job ----------

def test_state_dirs_on_macos(monkeypatch, tmp_path):
    monkeypatch.delenv("SMART_UPDATER_STATE_DIR", raising=False)
    monkeypatch.setattr(state, "is_macos", lambda: True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(state, "is_privileged", lambda: True)
    assert state.default_state_dir() == Path("/Library/Application Support/SmartUpdater")
    monkeypatch.setattr(state, "is_privileged", lambda: False)
    user = tmp_path / "Library" / "Application Support" / "SmartUpdater"
    assert state.default_state_dir() == user
    # a monitoring agent running as root must find a report written by either account
    assert state.candidate_state_dirs() == [Path("/Library/Application Support/SmartUpdater"), user]


def test_config_is_found_in_the_macos_locations_and_json_works_on_old_python(monkeypatch, tmp_path):
    monkeypatch.delenv("SMART_UPDATER_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "platform", "darwin")
    d = tmp_path / "Library" / "Application Support" / "SmartUpdater"
    d.mkdir(parents=True)
    (d / "config.json").write_text(json.dumps({"backends": {"brew": {"greedy_casks": True}}}))
    cfg = Config.load()
    assert cfg.source.endswith("SmartUpdater/config.json")
    assert cfg.backend_options("brew")["greedy_casks"] is True


def test_macos_built_in_rules_and_their_scope():
    cfg = Config()
    assert cfg.match_rule("macOS Sequoia 15.1-24B83", "macOS Sequoia 15.1", "softwareupdate").action == "manual"
    assert cfg.match_rule("Command Line Tools for Xcode-15.1", "Command Line Tools for Xcode", "softwareupdate") is None
    assert cfg.match_rule("docker-desktop", "", "brew").action == "manual"
    assert cfg.match_rule("python@3.12", "", "brew").max_bump == "patch"
    assert cfg.match_rule("postgresql@16", "", "brew").max_bump == "patch"
    # scoped to their own backend: an apt package that happens to match is not affected
    assert cfg.match_rule("macOS-thing", "", "apt") is None
    assert cfg.match_rule("python", "", "apt") is None


def test_brew_defaults_are_more_careful_than_apt():
    assert BrewBackend.default_max_bump == "minor" and BrewBackend.rollback_support == "none"
    plan_cfg = Config()
    b = BrewBackend(FakeRunner([]), NullEvents(), {})
    big = Update("brew", "wget", "1.21.3", "2.0.0", source="formula")
    verdict = Planner(plan_cfg, type("B", (), {
        "held": lambda s: set(), "norm": lambda s, n: n, "scheme": "generic", "stable_release": False,
        "default_max_bump": "minor", "manual_reason": lambda s, u: "", "hold_hint": lambda s, n: "",
    })(), NullEvents(), [big]).precheck(big)
    assert verdict.reason == Reason.BUMP_TOO_LARGE and "minor" in verdict.text
    assert b.default_max_bump == "minor"


def test_new_backends_are_registered_and_selectable():
    assert "brew" in ALL_NAMES and "softwareupdate" in ALL_NAMES


def test_launchd_plist_is_valid_and_runs_only_root_owned_things():
    data = plistlib.loads((ROOT / "examples" / "com.smartupdater.apply.plist").read_bytes())
    assert data["Label"] == "com.smartupdater.apply"
    args = data["ProgramArguments"]
    assert args[0] == "/usr/bin/python3" and args[1].endswith("smart-updater.pyz") and "apply" in args
    assert "/usr/bin/python3" == args[0] and not args[1].startswith(("/opt/homebrew", "/usr/local/bin"))
    path = data["EnvironmentVariables"]["PATH"]
    assert "homebrew" not in path and "/usr/local" not in path  # nothing a normal user can replace
    assert "Hour" in data["StartCalendarInterval"]


def test_json_example_config_loads_everywhere():
    cfg = Config.load(str(ROOT / "examples" / "config.example.json"))
    assert cfg.max_bump_for("brew", "x") == "minor"
    assert cfg.backend_options("brew")["greedy_casks"] is False
    assert cfg.match_rule("grub-pc", "", "apt").action == "allow"
