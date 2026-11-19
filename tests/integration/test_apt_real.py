"""Real apt, real dpkg, no mocks.

These build a throwaway local apt repository with toy ``su-toy-*`` packages,
install some of them for real, publish newer versions, and run Smart Updater
against it. They change the machine's dpkg database (and remove their packages
afterwards), so they only run when you ask for them:

    sudo SMART_UPDATER_INTEGRATION=1 python -m pytest tests/integration -v

Use a disposable VM or container.
"""
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from smart_updater import engine
from smart_updater.config import Config, HealthCheck
from smart_updater.models import Reason, Status

pytestmark = pytest.mark.skipif(
    os.environ.get("SMART_UPDATER_INTEGRATION") != "1"
    or not hasattr(os, "geteuid")
    or os.geteuid() != 0
    or not shutil.which("dpkg-deb")
    or not shutil.which("dpkg-scanpackages"),
    reason="set SMART_UPDATER_INTEGRATION=1 and run as root on a disposable Debian/Ubuntu machine",
)

TOYS = ["su-toy-app", "su-toy-plugin", "su-toy-core"]


class Repo:
    def __init__(self, root: Path):
        self.root = root
        self.dir = root / "repo"
        self.dir.mkdir()
        for sub in ("lists/partial", "cache/archives/partial", "none"):
            (root / sub).mkdir(parents=True)
        (root / "sources.list").write_text(f"deb [trusted=yes] file:{self.dir} ./\n")
        conf = root / "apt.conf"
        conf.write_text(
            textwrap.dedent(
                f"""
                Dir::Etc::sourcelist "{root}/sources.list";
                Dir::Etc::sourceparts "{root}/none";
                Dir::State::lists "{root}/lists";
                Dir::Cache "{root}/cache";
                Acquire::Languages "none";
                """
            )
        )
        self.env = dict(os.environ, APT_CONFIG=str(conf), DEBIAN_FRONTEND="noninteractive")

    def add(self, name, version, depends="", breaks="", postinst=""):
        pkg = self.root / "build" / f"{name}_{version}"
        (pkg / "DEBIAN").mkdir(parents=True)
        ctl = f"Package: {name}\nVersion: {version}\nArchitecture: all\nMaintainer: test <t@example.com>\nDescription: toy\n"
        if depends:
            ctl += f"Depends: {depends}\n"
        if breaks:
            ctl += f"Breaks: {breaks}\n"
        (pkg / "DEBIAN" / "control").write_text(ctl)
        if postinst:
            script = pkg / "DEBIAN" / "postinst"
            script.write_text("#!/bin/sh\nset -e\n" + postinst + "\n")
            script.chmod(0o755)
        subprocess.run(["dpkg-deb", "--build", str(pkg), str(self.dir / f"{name}_{version}_all.deb")], check=True, capture_output=True)

    def publish(self):
        with open(self.dir / "Packages", "w") as fh:
            subprocess.run(["dpkg-scanpackages", "-m", ".", "/dev/null"], cwd=self.dir, check=True, stdout=fh, stderr=subprocess.DEVNULL)
        subprocess.run(["apt-get", "update", "-q"], env=self.env, check=True, capture_output=True)

    def install(self, *specs):
        subprocess.run(["apt-get", "install", "-y", "--allow-downgrades", *specs], env=self.env, check=True, capture_output=True)

    @staticmethod
    def version(name):
        r = subprocess.run(["dpkg-query", "-W", "-f=${Version}", name], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = Repo(tmp_path)
    monkeypatch.setenv("APT_CONFIG", r.env["APT_CONFIG"])
    monkeypatch.setenv("DEBIAN_FRONTEND", "noninteractive")
    yield r
    subprocess.run(["dpkg", "--purge", "--force-depends", *TOYS], capture_output=True)


def apply(tmp_path, cfg=None):
    return engine.run_once(cfg or Config(), "apply", state_dir=str(tmp_path / "state"), quiet=True)


def by_name(report):
    return {d.name: d for d in report.decisions}


def test_patch_update_and_dependency_cascade_are_applied_together(repo, tmp_path):
    repo.add("su-toy-core", "1.0")
    repo.add("su-toy-plugin", "1.0", depends="su-toy-core (= 1.0)")
    repo.add("su-toy-app", "1.0")
    repo.publish()
    repo.install("su-toy-core=1.0", "su-toy-plugin=1.0", "su-toy-app=1.0")

    # core 2.0 breaks plugin 1.0; plugin 2.0 exists, so apt must move both together
    repo.add("su-toy-core", "2.0", breaks="su-toy-plugin (<< 2.0)")
    repo.add("su-toy-plugin", "2.0", depends="su-toy-core (= 2.0)")
    repo.add("su-toy-app", "1.0.1")
    repo.publish()

    plan = engine.run_once(Config(), "plan", state_dir=str(tmp_path / "state"), quiet=True)
    d = by_name(plan)
    assert {d[n].status for n in ("su-toy-core", "su-toy-plugin", "su-toy-app")} == {Status.WOULD_UPDATE}
    assert repo.version("su-toy-core") == "1.0"  # plan changed nothing

    report = apply(tmp_path)
    d = by_name(report)
    assert {d[n].status for n in d} == {Status.UPDATED}
    assert (repo.version("su-toy-core"), repo.version("su-toy-plugin"), repo.version("su-toy-app")) == ("2.0", "2.0", "1.0.1")
    assert report.status == "ok"


def test_update_that_would_remove_a_package_is_blocked_and_nothing_is_removed(repo, tmp_path):
    repo.add("su-toy-core", "1.0")
    repo.add("su-toy-plugin", "1.0", depends="su-toy-core (= 1.0)")
    repo.publish()
    repo.install("su-toy-core=1.0", "su-toy-plugin=1.0")
    repo.add("su-toy-core", "2.0", breaks="su-toy-plugin (<< 2.0)")  # but plugin has no 2.0
    repo.publish()

    report = apply(tmp_path)
    d = by_name(report)["su-toy-core"]
    assert (d.status, d.reason_code) == (Status.BLOCKED, Reason.WOULD_REMOVE)
    assert "su-toy-plugin" in d.reason
    assert (repo.version("su-toy-core"), repo.version("su-toy-plugin")) == ("1.0", "1.0")


def test_update_that_breaks_a_health_check_is_rolled_back(repo, tmp_path):
    marker = tmp_path / "service-broken"
    repo.add("su-toy-app", "1.0", postinst=f"rm -f {marker}")
    repo.publish()
    repo.install("su-toy-app=1.0")
    repo.add("su-toy-app", "1.1", postinst=f"touch {marker}")  # the new version "breaks the service"
    repo.publish()

    cfg = Config(health_checks=[HealthCheck("service", ["test", "!", "-e", str(marker)], timeout=5, retries=0)])
    report = apply(tmp_path, cfg)
    d = by_name(report)["su-toy-app"]
    assert (d.status, d.reason_code) == (Status.ROLLED_BACK, Reason.VERIFY_FAILED)
    assert repo.version("su-toy-app") == "1.0" and not marker.exists()
    assert report.status == "warning"

    again = apply(tmp_path, cfg)  # paused, not retried every run
    assert by_name(again)["su-toy-app"].reason_code == Reason.QUARANTINED
