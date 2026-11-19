from fake_backend import FakeBackend, NullEvents

from smart_updater.config import Config, Rule
from smart_updater.models import Reason, Status
from smart_updater.planner import Planner


def make(backend, cfg=None, quarantine=None):
    cfg = cfg or Config()
    return Planner(cfg, backend, NullEvents(), backend.list_updates(), quarantine)


def decision(plan, name):
    return plan.decisions[f"fake:{name}"]


def test_clean_update_is_planned():
    b = FakeBackend({"a": "1.0-1"}, {"a": "1.0-2"})
    plan = make(b).plan()
    assert len(plan.groups) == 1
    d = decision(plan, "a")
    assert (d.status, d.reason_code, d.attention) == (Status.WOULD_UPDATE, Reason.OK, None)


def test_admin_hold_is_respected_and_explained():
    b = FakeBackend({"a": "1.0", "b": "1.0"}, {"a": "1.1", "b": "1.1"}, held={"a"}, security={"b"})
    plan = make(b).plan()
    d = decision(plan, "a")
    assert (d.status, d.reason_code, d.attention) == (Status.SKIPPED, Reason.HELD, "info")
    assert "fake-unhold a" in d.suggested_action
    assert decision(plan, "b").status == Status.WOULD_UPDATE

    b2 = FakeBackend({"a": "1.0"}, {"a": "1.1"}, held={"a"}, security={"a"})
    assert decision(make(b2).plan(), "a").attention == "warning"  # a held *security* fix matters more


def test_builtin_rule_needs_human_with_reason():
    b = FakeBackend({"grub-pc": "2.06-1"}, {"grub-pc": "2.06-2"})
    d = decision(make(b).plan(), "grub-pc")
    assert (d.status, d.reason_code, d.attention) == (Status.BLOCKED, Reason.NEEDS_HUMAN, "warning")
    assert "boot" in d.reason.lower()


def test_user_allow_rule_overrides_builtin():
    b = FakeBackend({"grub-pc": "2.06-1"}, {"grub-pc": "2.06-2"})
    cfg = Config(rules=[Rule(["grub-pc"], "allow")])
    assert decision(make(b, cfg).plan(), "grub-pc").status == Status.WOULD_UPDATE


def test_bump_ceiling_blocks_and_suggests_rule():
    b = FakeBackend({"lib": "1.4.0"}, {"lib": "2.0.0"})
    b.default_max_bump = "minor"
    d = decision(make(b).plan(), "lib")
    assert (d.status, d.reason_code) == (Status.BLOCKED, Reason.BUMP_TOO_LARGE)
    assert 'max_bump = "major"' in d.suggested_action
    cfg = Config(rules=[Rule(["lib"], "allow", max_bump="major")])
    assert decision(make(b, cfg).plan(), "lib").status == Status.WOULD_UPDATE


def test_removal_is_resolved_by_upgrading_the_conflicting_package():
    # Upgrading A conflicts with the installed B. apt would remove B, but B has its own update.
    b = FakeBackend({"a": "1", "b": "1"}, {"a": "2", "b": "2"}, conflicts={"a": ["b"]})
    plan = make(b).plan()
    assert len(plan.groups) == 1
    assert sorted(plan.groups[0].members) == ["a", "b"]
    assert decision(plan, "a").status == decision(plan, "b").status == Status.WOULD_UPDATE
    assert any("remove" in c and "b" in c for c in decision(plan, "a").chain)


def test_unresolvable_removal_is_blocked_never_removed():
    b = FakeBackend({"a": "1", "c": "1"}, {"a": "2"}, conflicts={"a": ["c"]})
    d = decision(make(b).plan(), "a")
    assert (d.status, d.reason_code) == (Status.BLOCKED, Reason.WOULD_REMOVE)
    assert "c" in d.reason and d.attention == "info"


def test_removal_fix_blocked_by_policy_reports_dependency():
    b = FakeBackend({"a": "1", "b": "1"}, {"a": "2", "b": "2"}, conflicts={"a": ["b"]}, held={"b"})
    d = decision(make(b).plan(), "a")
    assert d.reason_code == Reason.DEPENDENCY_BLOCKED
    assert "held" in d.reason and "b" in d.reason


def test_failed_simulation_cascades_to_named_package():
    b = FakeBackend({"a": "1", "b": "1"}, {"a": "2", "b": "2"}, needs_explicit={"a": ["b"]})
    plan = make(b).plan()
    assert len(plan.groups) == 1 and sorted(plan.groups[0].members) == ["a", "b"]
    assert any("dry run failed" in c for c in decision(plan, "a").chain)


def test_failed_simulation_with_no_fix_is_blocked():
    b = FakeBackend({"a": "1"}, {"a": "2"}, requires={"a": ["ghost"]})
    d = decision(make(b).plan(), "a")
    assert (d.status, d.reason_code) == (Status.BLOCKED, Reason.SIMULATION_FAILED)


def test_cascade_depth_limit():
    inst = {n: "1" for n in "abcde"}
    avail = {n: "2" for n in "abcde"}
    conflicts = {"a": ["b"], "b": ["c"], "c": ["d"], "d": ["e"]}
    b = FakeBackend(inst, avail, conflicts=conflicts)
    cfg = Config(max_cascade_depth=2)
    d = decision(make(b, cfg).plan(), "a")
    assert d.reason_code == Reason.CASCADE_LIMIT
    # With a deep enough limit the whole chain resolves as one group.
    assert len(make(b, Config(max_cascade_depth=6)).plan().groups) == 1


def test_security_planned_first_and_covers_its_dependencies():
    b = FakeBackend({"s": "1", "n": "1"}, {"s": "2", "n": "2"}, requires={"s": ["n"]}, security={"s"})
    plan = make(b).plan()
    assert len(plan.groups) == 1  # n is covered by s's group, not a second group
    assert "required dependency" in decision(plan, "n").reason


def test_security_only_mode_skips_the_rest():
    b = FakeBackend({"s": "1", "n": "1"}, {"s": "2", "n": "2"}, security={"s"})
    plan = make(b, Config(security_only=True)).plan()
    assert decision(plan, "n").reason_code == Reason.SECURITY_ONLY
    assert decision(plan, "s").status == Status.WOULD_UPDATE


def test_per_run_limit_defers_the_rest():
    b = FakeBackend({n: "1" for n in "abc"}, {n: "2" for n in "abc"})
    plan = make(b, Config(max_updates_per_run=2)).plan()
    assert len(plan.groups) == 2
    assert [d.reason_code for d in plan.decisions.values()].count(Reason.LIMIT_REACHED) == 1


def test_quarantined_version_is_not_retried_until_it_changes():
    import time

    b = FakeBackend({"a": "1"}, {"a": "2"})
    q = {"fake:a:2": time.time() + 3600}
    d = decision(make(b, quarantine=q).plan(), "a")
    assert (d.status, d.reason_code, d.attention) == (Status.SKIPPED, Reason.QUARANTINED, "warning")
    b.available["a"] = "3"  # a newer version resets it
    assert decision(make(b, quarantine=q).plan(), "a").status == Status.WOULD_UPDATE
