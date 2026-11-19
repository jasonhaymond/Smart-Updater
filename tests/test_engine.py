"""Whole-run tests: engine + planner + executor + reporting, with a simulated backend."""
import json

import pytest
from fake_backend import FakeBackend, FakeHealth

from smart_updater import engine
from smart_updater.config import Config
from smart_updater.state import State


@pytest.fixture
def world(monkeypatch):
    holder = {}

    def install(backend):
        holder["b"] = backend
        monkeypatch.setattr(engine, "detect", lambda cfg, runner, events, only=None: [backend])
        monkeypatch.setattr(engine, "is_privileged", lambda: True)
        monkeypatch.setattr(engine, "windows_reboot_pending", lambda: [])
        # the real Health would probe this machine's systemd; use the simulated world instead
        monkeypatch.setattr(engine, "Health", lambda cfg, runner, events, backends: FakeHealth(backends[0]))
        return backend

    return install


def decisions(report):
    return {d.name: d for d in report.decisions}


def test_apply_run_end_to_end(world, tmp_path):
    b = world(
        FakeBackend(
            {"sec": "1", "dep": "1", "app": "1", "held": "1", "bad": "1"},
            {"sec": "2", "dep": "2", "app": "2", "held": "2", "bad": "2"},
            requires={"sec": ["dep"]}, held={"held"}, security={"sec", "held"}, breaking={("bad", "2")},
        )
    )
    r = engine.run_once(Config(), "apply", state_dir=str(tmp_path), quiet=True)
    d = decisions(r)
    assert (d["sec"].status.value, d["dep"].status.value, d["app"].status.value) == ("updated",) * 3
    assert d["bad"].status.value == "rolled_back" and b.installed["bad"] == "1"
    assert d["held"].status.value == "skipped"
    # The held security fix and the update that broke something both need a human; the first is warning-level.
    ids = {i.package: i.severity for i in r.attention}
    assert ids == {"held": "warning", "bad": "warning"}
    assert r.status == "warning" and r.exit_code == 1

    latest = json.loads((tmp_path / "latest.json").read_text())
    assert latest["schema"] == "smart-updater/report/v1" and latest["summary"]["updated"] == 3
    assert latest["attention"][0]["suggested_action"]  # every attention item says what to do
    run_dir = tmp_path / "runs" / r.run_id
    events = [json.loads(l) for l in (run_dir / "events.jsonl").read_text().splitlines()]
    assert {"run_start", "decision", "unit_start", "unit_ok", "unit_failed", "rollback_done", "run_end"} <= {e["event"] for e in events}
    assert (run_dir / "snapshots-fake.json").exists()
    # The failed version is paused, so a second run does not try it again.
    r2 = engine.run_once(Config(), "apply", state_dir=str(tmp_path), quiet=True)
    assert decisions(r2)["bad"].reason_code.value == "quarantined"
    assert len([c for c in b.apply_calls if "bad" in c]) == 1


def test_plan_run_changes_nothing_and_flags_pending_security(world, tmp_path):
    b = world(FakeBackend({"s": "1", "n": "1"}, {"s": "2", "n": "2"}, security={"s"}))
    r = engine.run_once(Config(), "plan", state_dir=str(tmp_path), quiet=True)
    assert b.apply_calls == [] and b.installed == {"s": "1", "n": "1"}
    assert r.status == "warning"  # a pending security update is something monitoring should see
    assert [i.package for i in r.attention] == ["s"] and r.attention[0].reason_code == "plan_only"


def test_security_item_goes_critical_after_the_grace_period(world, tmp_path):
    world(FakeBackend({"s": "1"}, {"s": "2"}, held={"s"}, security={"s"}))
    cfg = Config(critical_after_days=3)
    assert engine.run_once(cfg, "plan", state_dir=str(tmp_path), quiet=True).status == "warning"
    state = State(tmp_path)
    for k in state.data["first_seen"]:
        state.data["first_seen"][k] = "2020-01-01T00:00:00+00:00"
    state.save()
    r = engine.run_once(cfg, "plan", state_dir=str(tmp_path), quiet=True)
    assert r.status == "critical" and "now critical" in r.attention[0].reason


def test_backend_failure_is_reported_not_swallowed(world, tmp_path):
    class Broken(FakeBackend):
        def list_updates(self):
            raise engine.BackendError("repository unreachable")

    world(Broken({}, {}))
    cfg = Config()
    statuses = [engine.run_once(cfg, "plan", state_dir=str(tmp_path), quiet=True).status for _ in range(3)]
    assert statuses == ["warning", "warning", "critical"]  # repeated failures escalate


def test_rollback_run_restores_recorded_versions(world, tmp_path):
    b = world(FakeBackend({"a": "1"}, {"a": "2"}))
    r = engine.run_once(Config(), "apply", state_dir=str(tmp_path), quiet=True)
    assert b.installed["a"] == "2"
    msgs = engine.rollback_run(Config(), r.run_id, state_dir=str(tmp_path), quiet=True)
    assert b.installed["a"] == "1" and "restored" in msgs[0]
