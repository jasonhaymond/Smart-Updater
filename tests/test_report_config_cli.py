import json
import shutil
from datetime import datetime, timedelta

import pytest

from smart_updater import cli
from smart_updater.config import Config, ConfigError
from smart_updater.models import AttentionItem
from smart_updater.report import check_report, compute_status, finalize_items
from smart_updater.runner import ReadOnlyViolation, Runner
from smart_updater.state import RunLock, LockError, State


class NullEvents:
    def emit(self, *a, **k):
        pass


# ----------------------------------------------------------------- report -----

def item(sev="warning", security=True, esc=3, id="x:pkg:held"):
    return AttentionItem(id, sev, "pkg update blocked", "held", "why", security=security, escalate_after_days=esc if security else None)


def test_security_item_escalates_to_critical_with_age(tmp_path):
    state = State(tmp_path)
    cfg = Config(critical_after_days=3)
    now = datetime.now().astimezone()
    state.data["first_seen"]["x:pkg:held"] = (now - timedelta(days=4)).isoformat(timespec="seconds")
    it = item()
    finalize_items([it], state, cfg, ["x"], now)
    assert it.severity == "critical" and it.age_days >= 3.9
    assert compute_status([it]) == "critical"


def test_fresh_security_item_stays_warning_and_non_security_never_escalates(tmp_path):
    state = State(tmp_path)
    now = datetime.now().astimezone()
    state.data["first_seen"]["x:old:held"] = (now - timedelta(days=90)).isoformat(timespec="seconds")
    fresh, old = item(), item(security=False, id="x:old:held")
    finalize_items([fresh, old], state, Config(), ["x"], now)
    assert fresh.severity == "warning" and old.severity == "warning"
    assert compute_status([AttentionItem("i", "info", "t", "r", "w")]) == "ok"  # info never flips monitoring


def test_resolved_items_are_forgotten(tmp_path):
    state = State(tmp_path)
    state.data["first_seen"] = {"x:gone:held": "2020-01-01T00:00:00+00:00", "other:keep:held": "2020-01-01T00:00:00+00:00"}
    finalize_items([], state, Config(), ["x"])
    assert "x:gone:held" not in state.data["first_seen"] and "other:keep:held" in state.data["first_seen"]


def _report(status="warning", finished=None, mode="apply"):
    return {
        "status": status, "mode": mode, "finished": finished or datetime.now().astimezone().isoformat(timespec="seconds"),
        "summary": {"updated": 4, "failed": 0, "rolled_back": 0, "security_pending": 1, "attention": {"critical": 0, "warning": 1, "info": 2}},
        "attention": [{"severity": "warning", "title": "openssl 1 → 2: held back"}],
    }


def test_check_output_and_exit_codes():
    code, line = check_report(_report("warning"), 36)
    assert code == 1 and line.startswith("SMART_UPDATER WARNING") and "1 security" in line and "| attention=1 " in line
    assert check_report(_report("ok"), 36)[0] == 0
    assert check_report(_report("critical"), 36)[0] == 2
    old = (datetime.now().astimezone() - timedelta(hours=50)).isoformat(timespec="seconds")
    code, line = check_report(_report("ok", old), 36)
    assert code == 3 and "UNKNOWN" in line  # a monitor that stopped running must not look healthy
    assert check_report({"nonsense": 1}, 36)[0] == 3


# ----------------------------------------------------------------- config -----

def test_config_rejects_typos():
    with pytest.raises(ConfigError, match="unknown key"):
        Config.from_dict({"general": {"max_cascade_dept": 3}})
    with pytest.raises(ConfigError):
        Config.from_dict({"rules": [{"match": "x", "action": "maybe"}]})


def test_config_toml_roundtrip(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(
        '[general]\nbatch_size = 3\n[backends.pip]\nenabled = true\nenvironments = ["/v"]\n'
        '[[rules]]\nmatch = "postgresql-*"\naction = "manual"\nreason = "DB"\n'
        '[[health_checks]]\nname = "web"\ncommand = ["curl", "-f", "http://localhost"]\n'
    )
    cfg = Config.load(str(p))
    assert cfg.batch_size == 3 and cfg.backend_enabled("pip", False)
    assert cfg.match_rule("postgresql-16", "", "apt").action == "manual"
    assert cfg.match_rule("grub-pc", "", "apt").action == "manual"  # built-in still applies
    assert cfg.match_rule("curl", "", "apt") is None
    assert cfg.health_checks[0].command[0] == "curl"


# ----------------------------------------------------------- runner & lock -----

def test_read_only_runner_blocks_mutating_commands(tmp_path):
    r = Runner(NullEvents(), tmp_path, read_only=True)
    with pytest.raises(ReadOnlyViolation):
        r.run(["true"], mutating=True)
    assert r.run(["true"]).ok  # read-only commands are fine
    assert r.run(["definitely-not-a-command"]).rc == 127


def test_run_lock_is_exclusive_and_recovers_from_stale(tmp_path):
    path = tmp_path / "run.lock"
    with RunLock(path):
        with pytest.raises(LockError):
            RunLock(path).__enter__()
    assert not path.exists()
    path.write_text("999999999 1\n")  # dead pid
    with RunLock(path):
        pass


# -------------------------------------------------------------------- cli -----

def test_plan_end_to_end_on_this_machine(tmp_path, capsys):
    if not shutil.which("apt-get"):
        pytest.skip("needs a Debian-family machine")
    code = cli.main(["plan", "--state-dir", str(tmp_path), "--json", "-q"])
    data = json.loads(capsys.readouterr().out)
    assert data["schema"] == "smart-updater/report/v1" and data["mode"] == "plan"
    assert code == data["exit_code"] and data["status"] in ("ok", "warning", "critical")
    assert cli.main(["check", "--state-dir", str(tmp_path)]) in (0, 1, 2)
    assert (tmp_path / "latest.json").exists() and (tmp_path / "smart-updater.log").exists()
    run_dir = next((tmp_path / "runs").iterdir())
    assert (run_dir / "events.jsonl").exists() and (run_dir / "report.txt").exists()


def test_apply_refuses_without_privileges(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("smart_updater.engine.is_privileged", lambda: False)
    assert cli.main(["apply", "--state-dir", str(tmp_path), "-q"]) == 3
    assert "needs" in capsys.readouterr().err


def test_check_without_a_report_is_unknown(tmp_path, capsys):
    assert cli.main(["check", "--state-dir", str(tmp_path)]) == 3
    assert "UNKNOWN" in capsys.readouterr().out


def test_check_finds_the_report_in_another_users_default_location(tmp_path, monkeypatch, capsys):
    """The updater runs as root; the monitoring agent runs as someone else and has no state-dir setting."""
    system_dir, user_dir = tmp_path / "system", tmp_path / "user"
    system_dir.mkdir(), user_dir.mkdir()
    monkeypatch.delenv("SMART_UPDATER_STATE_DIR", raising=False)
    monkeypatch.setattr(cli, "candidate_state_dirs", lambda: [system_dir, user_dir])
    monkeypatch.setattr(cli, "default_state_dir", lambda: user_dir)
    data = _report("ok")
    (system_dir / "latest.json").write_text(json.dumps(data))
    assert cli.main(["check"]) == 0
    assert "SMART_UPDATER OK" in capsys.readouterr().out
    assert cli.main(["check", "--state-dir", str(user_dir)]) == 3  # an explicit directory is never second-guessed
