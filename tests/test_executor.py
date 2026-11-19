import pytest
from fake_backend import FakeBackend, FakeHealth, NullEvents

from smart_updater.config import Config
from smart_updater.executor import Executor, make_units
from smart_updater.models import Reason, Status
from smart_updater.planner import Planner
from smart_updater.state import State


def run(backend, cfg=None, tmp=None):
    cfg = cfg or Config()
    updates = backend.list_updates()
    planner = Planner(cfg, backend, NullEvents(), updates, {})
    plan = planner.plan()
    health = FakeHealth(backend)
    state = State(tmp)
    ex = Executor(cfg, backend, planner, health, NullEvents(), state, tmp, health.snapshot())
    ex.run(plan)
    return plan.decisions, state, ex


def d(decisions, name):
    return decisions[f"fake:{name}"]


def test_successful_update_is_verified(tmp_path):
    b = FakeBackend({"a": "1"}, {"a": "2"})
    decisions, _, _ = run(b, tmp=tmp_path)
    assert (d(decisions, "a").status, d(decisions, "a").attention) == (Status.UPDATED, None)
    assert b.installed["a"] == "2"
    assert (tmp_path / "snapshots-fake.json").exists()  # a rollback record exists for every apply


def test_health_regression_triggers_rollback_and_quarantine(tmp_path):
    b = FakeBackend({"a": "1"}, {"a": "2"}, breaking={("a", "2")})
    decisions, state, _ = run(b, tmp=tmp_path)
    x = d(decisions, "a")
    assert (x.status, x.reason_code, x.attention) == (Status.ROLLED_BACK, Reason.VERIFY_FAILED, "warning")
    assert "service" in x.reason and "Rolled back" in x.reason
    assert b.installed["a"] == "1"
    assert "fake:a:2" in state.data["quarantine"]


def test_apply_failure_is_rolled_back_and_reported(tmp_path):
    b = FakeBackend({"a": "1"}, {"a": "2"}, fail_apply={"a"})
    decisions, _, _ = run(b, tmp=tmp_path)
    assert (d(decisions, "a").status, d(decisions, "a").reason_code) == (Status.FAILED, Reason.APPLY_FAILED)
    assert b.rollback_calls and b.installed["a"] == "1"


def test_failed_rollback_is_critical(tmp_path):
    b = FakeBackend({"a": "1"}, {"a": "2"}, breaking={("a", "2")}, rollback_fails=True)
    decisions, _, _ = run(b, tmp=tmp_path)
    x = d(decisions, "a")
    assert (x.status, x.reason_code, x.attention) == (Status.FAILED, Reason.ROLLBACK_FAILED, "critical")
    assert "NOT" in x.reason


def test_dependency_group_rolls_back_together(tmp_path):
    b = FakeBackend({"a": "1", "b": "1"}, {"a": "2", "b": "2"}, conflicts={"a": ["b"]}, breaking={("b", "2")})
    decisions, _, _ = run(b, tmp=tmp_path)
    assert b.installed == {"a": "1", "b": "1"}  # neither half stays applied
    assert d(decisions, "a").status == d(decisions, "b").status == Status.ROLLED_BACK


def test_batch_failure_splits_so_good_updates_still_land(tmp_path):
    b = FakeBackend({"good": "1", "bad": "1"}, {"good": "2", "bad": "2"}, breaking={("bad", "2")})
    decisions, _, _ = run(b, Config(batch_size=5), tmp_path)
    assert d(decisions, "good").status == Status.UPDATED
    assert d(decisions, "bad").status == Status.ROLLED_BACK
    assert b.installed == {"good": "2", "bad": "1"}
    assert len(b.apply_calls[0]) == 2  # first tried as one batch


def test_run_aborts_after_repeated_failures(tmp_path):
    names = ["a", "b", "c"]
    b = FakeBackend({n: "1" for n in names}, {n: "2" for n in names}, breaking={(n, "2") for n in names})
    decisions, _, _ = run(b, Config(max_failures=2), tmp_path)
    assert d(decisions, "a").status == d(decisions, "b").status == Status.ROLLED_BACK
    assert (d(decisions, "c").status, d(decisions, "c").reason_code) == (Status.SKIPPED, Reason.RUN_ABORTED)
    assert b.installed["c"] == "1"


def test_state_change_between_plan_and_apply_is_caught(tmp_path):
    b = FakeBackend({"a": "1", "c": "1"}, {"a": "2"})
    cfg = Config()
    planner = Planner(cfg, b, NullEvents(), b.list_updates(), {})
    plan = planner.plan()
    b.conflicts["a"] = ["c"]  # the world changes after planning: now applying would remove c
    health = FakeHealth(b)
    Executor(cfg, b, planner, health, NullEvents(), State(tmp_path), tmp_path, health.snapshot()).run(plan)
    x = plan.decisions["fake:a"]
    assert x.status == Status.BLOCKED and "Re-checked" in x.reason
    assert b.apply_calls == [] and b.installed["a"] == "1"


def test_make_units_never_batches_overlapping_groups():
    from smart_updater.planner import Group

    mk = lambda *m: Group([], None, [], list(m))  # noqa: E731
    groups = [mk("a"), mk("b"), mk("b", "c"), mk("d")]
    units = make_units(groups, 10)
    assert [[g.members for g in u] for u in units] == [[["a"], ["b"]], [["b", "c"], ["d"]]]
    assert len(make_units(groups, 1)) == 4
