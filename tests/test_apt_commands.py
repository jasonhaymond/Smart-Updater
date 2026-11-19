"""The exact commands the apt backend runs matter: they are the safety net under the planner."""
from fake_backend import NullEvents

from smart_updater.backends.apt import AptBackend
from smart_updater.models import SimResult, Update
from smart_updater.runner import CmdResult


class RecordingRunner:
    def __init__(self, stdout="", rc=0):
        self.calls = []
        self.stdout, self.rc = stdout, rc

    def run(self, cmd, **kw):
        self.calls.append((list(cmd), kw))
        return CmdResult(list(cmd), self.rc, self.stdout, "")


def backend(**kw):
    r = RecordingRunner(**kw)
    b = AptBackend(r, NullEvents(), {})
    b._native = "amd64"
    return b, r


def test_apply_never_removes_pins_versions_and_keeps_local_config():
    b, r = backend()
    b.apply(SimResult(True, upgrades={"libssl3t64": "3.0.13-0ubuntu3.5", "libc6:i386": "2.39-1"}))
    cmd, kw = r.calls[0]
    assert cmd[:2] == ["apt-get", "install"]
    assert "--no-remove" in cmd and "--only-upgrade" in cmd and "-y" in cmd
    assert "libssl3t64=3.0.13-0ubuntu3.5" in cmd and "libc6:i386=2.39-1" in cmd  # exactly what was simulated
    assert "Dpkg::Options::=--force-confold" in cmd  # never overwrite the admin's edited config files
    assert kw["mutating"] is True and kw["env"]["DEBIAN_FRONTEND"] == "noninteractive"


def test_rollback_downgrades_but_still_never_removes():
    b, r = backend()
    b.rollback({"versions": {"libssl3t64": "3.0.13-0ubuntu3.4"}})
    cmd, kw = r.calls[0]
    assert "--allow-downgrades" in cmd and "--no-remove" in cmd and "libssl3t64=3.0.13-0ubuntu3.4" in cmd
    assert kw["mutating"] is True


def test_simulation_is_a_real_dry_run_and_not_mutating():
    b, r = backend(stdout="Inst a [1] (2 x [amd64])\n")
    sim = b.simulate([Update("apt", "a", "1", "2")])
    cmd, kw = r.calls[0]
    assert cmd[:3] == ["apt-get", "-s", "-o"] and "a=2" in cmd
    assert not kw.get("mutating") and sim.ok and sim.upgrades == {"a": "2"}


def test_simulation_that_plans_nothing_is_a_failure_not_success():
    b, _ = backend(stdout="0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n")
    sim = b.simulate([Update("apt", "a", "1", "2")])
    assert not sim.ok and "did not plan an upgrade" in sim.error
