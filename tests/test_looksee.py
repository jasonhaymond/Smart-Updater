"""The Looksee integration: what `check` prints for its Custom-script check, and pushing to a Heartbeat URL.

Looksee's agent (agent/internal/checks/script.go) takes the exit code as the status, the first output
line as the message, the FIRST `label=value` after a `|` as the value, and keeps the first 4096
characters of output, which the dashboard shows as "Latest details". These tests pin those contracts.
"""
import io
import json
import re
import subprocess
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from smart_updater import cli, engine, looksee
from smart_updater.config import Config, ConfigError
from smart_updater.report import check_details, check_report

ROOT = Path(__file__).resolve().parent.parent
WRAPPERS = ROOT / "integrations" / "looksee"

# The same expression Looksee's agent uses to find performance data.
LOOKSEE_PERFDATA = re.compile(r"'?([^'=]+)'?=(-?[\d.]+)")


def item(sev="warning", **kw):
    base = {
        "id": f"apt:pkg{kw.get('n', 0)}:needs_human", "severity": sev, "title": "openssl 3.0 → 3.1: needs a person",
        "reason_code": "needs_human", "reason": "A rule says this package needs a person to approve the update.",
        "suggested_action": "Update it yourself when you can watch it.", "security": False, "age_days": 0.2,
    }
    base.update({k: v for k, v in kw.items() if k != "n"})
    return base


def report(items, status="warning", mode="apply"):
    crit = sum(i["severity"] == "critical" for i in items)
    warn = sum(i["severity"] == "warning" for i in items)
    info = sum(i["severity"] == "info" for i in items)
    return {
        "status": status, "mode": mode, "finished": datetime.now().astimezone().isoformat(timespec="seconds"),
        "summary": {"updated": 2, "failed": 0, "rolled_back": 0, "security_pending": sum(bool(i.get("security")) for i in items),
                    "attention": {"critical": crit, "warning": warn, "info": info}},
        "attention": items,
    }


# --------------------------------------------------- what Looksee's agent will see ----------

def test_the_first_perfdata_value_is_the_number_that_needs_a_person():
    _, line = check_report(report([item(), item("critical", n=1), item("info", n=2)], "critical"), 36)
    perf = line.split("|", 1)[1]
    label, value = LOOKSEE_PERFDATA.search(perf).groups()
    assert label.strip() == "attention" and float(value) == 2  # critical + warning, not info


def test_details_list_each_item_with_why_next_and_id_most_severe_first():
    data = report([item(n=1, title="b warn"), item("critical", n=2, title="a crit", security=True, age_days=5.4), item("info", n=3)])
    lines = check_details(data)
    text = "\n".join(lines)
    assert lines[0].startswith("[CRITICAL] [SECURITY] a crit") and "(open 5d)" in lines[0]
    assert text.index("a crit") < text.index("b warn")
    assert "  why:  A rule says" in text and "  next: Update it yourself" in text and "  id:   apt:pkg2:needs_human" in text
    assert "1 informational item(s)" in lines[-1]
    assert "→" not in text  # plain ASCII survives any code page


def test_details_stay_inside_the_4096_characters_looksee_keeps():
    items = [item(n=i, reason="x" * 600, suggested_action="y" * 600, title="t" * 400) for i in range(60)]
    lines = check_details(report(items))
    total = sum(len(l) + 1 for l in lines)
    assert total < 3900
    assert "more not shown here" in lines[-1] and re.search(r"\+(\d+) more", lines[-1])
    shown = sum(1 for l in lines if l.startswith("[WARNING]"))
    assert shown >= 4 and int(re.search(r"\+(\d+) more", lines[-1]).group(1)) == 60 - shown


def test_check_details_flag_prints_after_the_status_line_and_keeps_the_exit_code(tmp_path, capsys):
    (tmp_path / "latest.json").write_text(json.dumps(report([item(), item("critical", n=1)], "critical")))
    code = cli.main(["check", "--details", "--state-dir", str(tmp_path)])
    out = capsys.readouterr().out.splitlines()
    assert code == 2 and out[0].startswith("SMART_UPDATER CRITICAL") and "[CRITICAL]" in out[1]
    # without the flag the output is exactly one line, as before
    cli.main(["check", "--state-dir", str(tmp_path)])
    assert len(capsys.readouterr().out.splitlines()) == 1


def test_a_stale_or_missing_report_gives_unknown_and_no_details(tmp_path, capsys):
    assert cli.main(["check", "--details", "--state-dir", str(tmp_path)]) == 3
    assert capsys.readouterr().out.startswith("SMART_UPDATER UNKNOWN")


def test_output_with_arrows_does_not_crash_on_a_legacy_windows_code_page(monkeypatch):
    """A piped stdout on Windows can be cp1252, which cannot encode the arrows in reports."""
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252", errors="strict"))
    cli._safe_streams()
    print("openssl 3.0 → 3.1")
    sys.stdout.flush()
    assert raw.getvalue().startswith(b"openssl 3.0 ")


# ------------------------------------------------------------------- the wrappers ----------

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")
def test_shell_wrapper_passes_through_exit_code_and_output(tmp_path):
    (tmp_path / "latest.json").write_text(json.dumps(report([item("critical")], "critical")))
    fake = tmp_path / "smart-updater"
    fake.write_text('#!/bin/sh\necho "ARGS: $*"\nexit 2\n')
    fake.chmod(0o755)
    out = subprocess.run(
        ["/bin/sh", str(WRAPPERS / "smart-updater-check.sh"), "--max-age-hours", "48"],
        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "SMART_UPDATER_BIN": str(fake)},
    )
    assert out.returncode == 2
    assert out.stdout.strip() == "ARGS: check --details --max-age-hours 48"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")
def test_shell_wrapper_says_unknown_when_nothing_is_installed(tmp_path):
    out = subprocess.run(["/bin/sh", str(WRAPPERS / "smart-updater-check.sh")], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin"})
    assert out.returncode == 3 and out.stdout.startswith("SMART_UPDATER UNKNOWN") and "not installed" in out.stdout


def test_wrapper_file_names_pass_looksees_script_name_rule():
    for f in WRAPPERS.iterdir():
        if f.suffix in (".sh", ".ps1"):
            assert re.fullmatch(r"[A-Za-z0-9._-]+", f.name)


def test_powershell_wrapper_is_there_and_returns_the_exit_code():
    text = (WRAPPERS / "smart-updater-check.ps1").read_text()
    assert "--details" in text and "exit $LASTEXITCODE" in text and "exit 3" in text
    assert "$env:ProgramFiles" in text  # an administrator-only location, since the agent runs as SYSTEM


# ------------------------------------------------------------- pushing to a Heartbeat ----------

class Sink(BaseHTTPRequestHandler):
    seen = []
    answer = 200

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        Sink.seen.append({"path": self.path, "body": json.loads(body), "type": self.headers.get("Content-Type")})
        self.send_response(Sink.answer)
        self.end_headers()
        self.wfile.write(b"OK" if Sink.answer == 200 else b"Unknown push token")

    def log_message(self, *a):
        pass


@pytest.fixture
def looksee_server():
    Sink.seen, Sink.answer = [], 200
    srv = HTTPServer(("127.0.0.1", 0), Sink)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/api/hb/SECRET-TOKEN-123"
    srv.shutdown()


def test_payload_maps_status_and_value():
    p = looksee.build_payload(report([item(), item("critical", n=1)], "critical"))
    assert p["status"] == "down" and p["value"] == 2 and p["msg"].startswith("SMART_UPDATER CRITICAL") and "|" not in p["msg"]
    assert looksee.build_payload(report([], "ok"))["status"] == "up"
    assert looksee.build_payload(report([item()], "warning"))["status"] == "warn"
    stale = report([], "ok")
    stale["finished"] = "2020-01-01T00:00:00+00:00"
    assert looksee.build_payload(stale)["status"] == "warn"  # silence must never look healthy
    assert len(looksee.build_payload(report([item(title="t" * 3000)], "warning"))["msg"]) <= 900


def test_push_posts_json_to_the_heartbeat_url(looksee_server):
    ok, detail = looksee.push(looksee_server, {"status": "warn", "msg": "hi", "value": 2})
    assert ok and detail == "HTTP 200"
    assert Sink.seen == [{"path": "/api/hb/SECRET-TOKEN-123", "body": {"status": "warn", "msg": "hi", "value": 2}, "type": "application/json"}]


def test_a_rejected_token_is_reported_without_revealing_it(looksee_server):
    Sink.answer = 404
    ok, detail = looksee.push(looksee_server, {"status": "up"})
    assert not ok and "404" in detail and "regenerated" in detail and "SECRET-TOKEN" not in detail


def test_unreachable_looksee_never_raises_and_never_leaks_the_url():
    ok, detail = looksee.push("http://127.0.0.1:9/api/hb/SECRET-TOKEN-123", {"status": "up"}, timeout=2)
    assert not ok and "SECRET-TOKEN" not in detail
    assert looksee.push("ftp://x/y", {})[0] is False and looksee.push("not a url", {})[0] is False


def test_url_comes_from_env_or_a_file_and_the_env_wins(monkeypatch, tmp_path):
    f = tmp_path / "url"
    f.write_text("  https://looksee.example/api/hb/T1\n")
    cfg = Config.from_dict({"looksee": {"push_url_file": str(f)}})
    monkeypatch.delenv(looksee.ENV_URL, raising=False)
    assert looksee.load_url(cfg) == "https://looksee.example/api/hb/T1"
    monkeypatch.setenv(looksee.ENV_URL, "https://other/api/hb/T2")
    assert looksee.load_url(cfg) == "https://other/api/hb/T2"
    monkeypatch.delenv(looksee.ENV_URL)
    assert looksee.load_url(Config()) is None
    with pytest.raises(ValueError, match="cannot read"):
        looksee.load_url(Config.from_dict({"looksee": {"push_url_file": str(tmp_path / "missing")}}))


def test_config_rejects_unknown_looksee_keys():
    with pytest.raises(ConfigError, match="looksee"):
        Config.from_dict({"looksee": {"url": "https://secret-in-the-config"}})


def test_push_command_end_to_end(looksee_server, tmp_path, monkeypatch, capsys):
    (tmp_path / "latest.json").write_text(json.dumps(report([item()], "warning")))
    urlfile = tmp_path / "url"
    urlfile.write_text(looksee_server)
    monkeypatch.delenv(looksee.ENV_URL, raising=False)
    assert cli.main(["push", "--state-dir", str(tmp_path), "--url-file", str(urlfile)]) == 0
    out = capsys.readouterr().out
    assert "status=warn value=1" in out and "SECRET-TOKEN" not in out  # the URL is never printed
    assert Sink.seen[0]["body"]["status"] == "warn"
    assert cli.main(["push", "--state-dir", str(tmp_path)]) == 3  # no URL configured
    assert "no Looksee URL" in capsys.readouterr().err


def test_apply_runs_push_and_plan_runs_do_not(monkeypatch, tmp_path, looksee_server):
    from fake_backend import FakeBackend, FakeHealth

    monkeypatch.setattr(engine, "detect", lambda cfg, runner, events, only=None: [FakeBackend({"a": "1"}, {"a": "2"})])
    monkeypatch.setattr(engine, "is_privileged", lambda: True)
    monkeypatch.setattr(engine, "windows_reboot_pending", lambda: [])
    monkeypatch.setattr(engine, "Health", lambda cfg, runner, events, backends: FakeHealth(backends[0]))
    monkeypatch.setenv(looksee.ENV_URL, looksee_server)
    engine.run_once(Config(), "plan", state_dir=str(tmp_path), quiet=True)
    assert Sink.seen == []  # a plan-only run must not make a stopped apply schedule look alive
    engine.run_once(Config(), "apply", state_dir=str(tmp_path), quiet=True)
    assert len(Sink.seen) == 1 and Sink.seen[0]["body"]["status"] == "up" and Sink.seen[0]["body"]["value"] == 0


def test_a_failing_push_cannot_break_a_run(monkeypatch, tmp_path):
    from fake_backend import FakeBackend, FakeHealth

    b = FakeBackend({"a": "1"}, {"a": "2"})
    monkeypatch.setattr(engine, "detect", lambda cfg, runner, events, only=None: [b])
    monkeypatch.setattr(engine, "is_privileged", lambda: True)
    monkeypatch.setattr(engine, "windows_reboot_pending", lambda: [])
    monkeypatch.setattr(engine, "Health", lambda cfg, runner, events, backends: FakeHealth(backends[0]))
    monkeypatch.setenv(looksee.ENV_URL, "http://127.0.0.1:9/api/hb/SECRET-TOKEN-123")
    r = engine.run_once(Config(), "apply", state_dir=str(tmp_path), quiet=True)
    assert r.status == "ok" and b.installed["a"] == "2"
    log = (tmp_path / "smart-updater.log").read_text()
    assert "could not tell Looksee" in log and "SECRET-TOKEN" not in log
