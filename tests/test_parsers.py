import json

from smart_updater import osv
from smart_updater.backends import apt, dnf, pip, winget
from smart_updater.models import Update

# ---------------------------------------------------------------- apt ---------

APT_LIST = """Listing...
libssl3t64/noble-updates,noble-security 3.0.13-0ubuntu3.5 amd64 [upgradable from: 3.0.13-0ubuntu3.4]
vim/noble-updates 2:9.1.0016-1ubuntu7.4 amd64 [upgradable from: 2:9.1.0016-1ubuntu7.3]
libc6/noble-updates 2.39-0ubuntu8.3 i386 [upgradable from: 2.39-0ubuntu8.2]
"""

APT_SIM = """Reading package lists...
The following packages have been kept back:
  linux-generic linux-image-generic
Inst libssl3t64 [3.0.13-0ubuntu3.4] (3.0.13-0ubuntu3.5 Ubuntu:24.04/noble-updates, Ubuntu:24.04/noble-security [amd64])
Inst libnewdep1 (1.2-1 Ubuntu:24.04/noble [amd64])
Inst libc6:i386 [2.39-0ubuntu8.2] (2.39-0ubuntu8.3 Ubuntu:24.04/noble-updates [i386]) []
Remv oldthing [1.0-1]
Conf libssl3t64 (3.0.13-0ubuntu3.5 Ubuntu:24.04/noble-updates [amd64])
"""

APT_ERR = """The following packages have unmet dependencies:
 libfoo : Breaks: bar (< 2.0) but 1.9 is to be installed
E: Unable to correct problems, you have held broken packages.
"""


def test_apt_upgradable_parse_flags_security_and_multiarch():
    ups = apt.parse_upgradable(APT_LIST, "amd64")
    by = {u.name: u for u in ups}
    assert by["libssl3t64"].security and not by["vim"].security
    assert by["libssl3t64"].installed == "3.0.13-0ubuntu3.4" and by["libssl3t64"].candidate == "3.0.13-0ubuntu3.5"
    assert "libc6:i386" in by  # foreign architecture keeps its qualifier


def test_apt_simulation_parse():
    p = apt.parse_simulation(APT_SIM, "amd64")
    assert p["upgrades"] == {"libssl3t64": "3.0.13-0ubuntu3.5", "libc6:i386": "2.39-0ubuntu8.3"}
    assert p["previous"]["libssl3t64"] == "3.0.13-0ubuntu3.4"
    assert p["installs"] == {"libnewdep1": "1.2-1"}
    assert p["removals"] == ["oldthing"]
    assert p["held_back"] == ["linux-generic", "linux-image-generic"]


def test_apt_error_parse_names_blockers():
    msg, blockers = apt.parse_errors(APT_ERR)
    assert "Breaks: bar" in msg and "libfoo" in blockers and "bar" in blockers


# ---------------------------------------------------------------- pip ---------

def test_pip_outdated_and_report():
    ups = pip.parse_outdated(json.dumps([{"name": "Requests", "version": "2.31.0", "latest_version": "2.32.3"}]), "pip@/v")
    assert ups[0].name == "requests" and ups[0].display == "Requests"
    report = {
        "install": [
            {"metadata": {"name": "requests", "version": "2.32.3"}},
            {"metadata": {"name": "urllib3", "version": "2.2.2"}},
            {"metadata": {"name": "Brand_New", "version": "1.0"}},
            {"metadata": {"name": "same", "version": "1.0"}},
        ]
    }
    upgrades, previous, installs = pip.parse_report(
        json.dumps(report), {"requests": "2.31.0", "urllib3": "2.0.7", "same": "1.0"}
    )
    assert upgrades == {"requests": "2.32.3", "urllib3": "2.2.2"}
    assert previous["urllib3"] == "2.0.7" and installs == {"brand-new": "1.0"}


def test_pip_conflict_names():
    err = (
        "ERROR: Cannot install a==2.0 and b because these package versions have conflicting dependencies.\n"
        "The conflict is caused by:\n    a 2.0 depends on c<2\n    b 1.0 depends on c>=2\n"
    )
    names = pip.parse_conflict_names(err)
    assert "a" in names and "c" in names


# ---------------------------------------------------------------- dnf ---------

DNF_CHECK = """
kernel.x86_64                     6.8.5-301.fc40               updates
openssl-libs.x86_64               1:3.2.1-2.fc40               updates

Obsoleting Packages
foo.x86_64                        1.0-1.fc40                   updates
"""

DNF_UPDATEINFO = """FEDORA-2026-abc123 Important/Sec. openssl-libs-1:3.2.1-2.fc40.x86_64
FEDORA-2026-def456 Moderate/Sec.  kernel-6.8.5-301.fc40.x86_64
"""

DNF_TXN = """Dependencies resolved.
================================================================================
 Package            Arch        Version                 Repository         Size
================================================================================
Upgrading:
 openssl-libs       x86_64      1:3.2.1-2.fc40          updates           2.3 M
Installing dependencies:
 libfoo             x86_64      1.0-1.fc40              updates            12 k
Removing dependent packages:
 oldapp             x86_64      1.0-1.fc40              @System           1.0 M

Transaction Summary
================================================================================
Upgrade  1 Package
Operation aborted.
"""


def test_dnf_parsers():
    ups = dnf.parse_check_update(DNF_CHECK)
    assert [u.name for u in ups] == ["kernel", "openssl-libs"]  # stops at "Obsoleting Packages"
    assert ups[1].candidate == "1:3.2.1-2.fc40"
    adv = dnf.parse_updateinfo(DNF_UPDATEINFO)
    assert adv == {"openssl-libs": ["FEDORA-2026-abc123"], "kernel": ["FEDORA-2026-def456"]}
    t = dnf.parse_transaction(DNF_TXN)
    assert t["upgrades"] == {"openssl-libs": "1:3.2.1-2.fc40"}
    assert t["installs"] == {"libfoo": "1.0-1.fc40"} and t["removals"] == ["oldapp"]


# ------------------------------------------------------------- winget ---------

def _table(rows):
    header = ["Name", "Id", "Version", "Available", "Source"]
    widths = [30, 28, 14, 14, 8]
    fmt = lambda cells: "".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip()  # noqa: E731
    return "\n".join(["   - ", fmt(header), "-" * sum(widths)] + [fmt(r) for r in rows] + [f"{len(rows)} upgrades available."])


def test_winget_table_parse_and_safety_skips():
    text = _table(
        [
            ["7-Zip", "7zip.7zip", "23.01", "24.08", "winget"],
            ["Mozilla Firefox (x64 en-US)", "Mozilla.Firefox", "126.0", "130.0", "winget"],
            ["Mystery", "Vendor.Mystery", "Unknown", "2.0", "winget"],
            ["Cut Off", "Some.Very.Long.Package…", "1.0", "2.0", "winget"],
        ]
    )
    rows = winget.parse_table(text)
    assert len(rows) == 4 and rows[1]["Id"] == "Mozilla.Firefox" and rows[1]["Available"] == "130.0"
    ups, skipped = winget.rows_to_updates(rows)
    assert [u.name for u in ups] == ["7zip.7zip", "Mozilla.Firefox"]
    assert ups[0].display == "7-Zip" and ups[0].installed == "23.01" and ups[0].candidate == "24.08"
    assert len(skipped) == 2 and any("truncated" in s for s in skipped) and any("Unknown" in s for s in skipped)


def test_winget_no_table_returns_none():
    assert winget.parse_table("No installed package found matching input criteria.") is None


# --------------------------------------------------------------- osv ----------

def test_osv_marks_fixing_updates_and_lists_unfixable():
    vulns = {("flask", "1.0"): ["GHSA-1"], ("django", "2.0"): ["GHSA-2"], ("django", "2.0.1"): ["GHSA-2"]}

    def fake_post(url, body):
        return {"results": [{"vulns": [{"id": i} for i in vulns.get((q["package"]["name"], q["version"]), [])]} for q in body["queries"]]}

    ups = [Update("pip@/v", "flask", "1.0", "1.1"), Update("pip@/v", "django", "2.0", "2.0.1")]
    no_fix, ok = osv.classify("PyPI", {"flask": "1.0", "django": "2.0", "safe": "1.0"}, ups, fake_post)
    assert ok
    assert ups[0].security and ups[0].advisories == ["GHSA-1"]  # update fixes it
    assert not ups[1].security  # the "fix" is still vulnerable
    assert [n["name"] for n in no_fix] == ["django"]


def test_osv_unreachable_changes_nothing():
    def boom(url, body):
        raise OSError("no network")

    ups = [Update("pip@/v", "flask", "1.0", "1.1")]
    assert osv.classify("PyPI", {"flask": "1.0"}, ups, boom) == ([], False)
    assert not ups[0].security
