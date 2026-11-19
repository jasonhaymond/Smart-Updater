"""Documentation drift guards: shipped examples must load, and docs must name real things."""
import re
import sys
from pathlib import Path

import pytest

from smart_updater.config import Config
from smart_updater.models import REASON_HELP

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(sys.version_info < (3, 11), reason="TOML needs Python 3.11+")
def test_example_config_loads_and_means_what_it_says():
    cfg = Config.load(str(ROOT / "examples" / "config.example.toml"))
    assert cfg.max_bump_for("pip", "major") == "minor"
    assert cfg.match_rule("postgresql-16", "", "apt").action == "manual"
    assert cfg.match_rule("Google.Chrome", "", "winget").max_bump == "major"
    assert cfg.match_rule("grub-pc", "", "apt").action == "allow"  # user rule beats the built-in one
    assert cfg.health_checks[0].command[0] == "curl"


def test_every_reason_code_in_the_readme_exists():
    readme = (ROOT / "README.md").read_text()
    known = {r.value for r in REASON_HELP}
    section = readme.split("### Why something was not updated")[1].split("## Exit codes")[0]
    mentioned = set(re.findall(r"`([a-z_]+)`", section))
    assert mentioned <= known, f"README mentions unknown reason codes: {mentioned - known}"


def test_build_script_produces_a_working_single_file(tmp_path):
    import subprocess

    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_pyz.py")], check=True, capture_output=True)
    pyz = ROOT / "dist" / "smart-updater.pyz"
    out = subprocess.run([sys.executable, str(pyz), "--version"], capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.startswith("smart-updater ")
