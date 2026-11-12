"""The bpkit command line."""

import os
import tomllib
from pathlib import Path

import pytest

from bpkit.cli import main

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
FLEET = str(EXAMPLES / "fleet.py")


def test_render_class_to_stdout(capsys):
    assert main(["render", f"{FLEET}:StagingWeb"]) == 0
    data = tomllib.loads(capsys.readouterr().out)
    assert data["name"] == "staging-web"


def test_render_class_to_file(tmp_path):
    out = tmp_path / "x.toml"
    assert main(["render", f"{FLEET}:WebServer", "-o", str(out)]) == 0
    assert tomllib.loads(out.read_text())["name"] == "web-server"


def test_render_module_skips_abstract(tmp_path):
    assert main(["render", FLEET, "-o", str(tmp_path)]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "minimal-web.toml",
        "staging-web.toml",
        "web-server.toml",
    ]


def test_render_dotted_module(tmp_path, monkeypatch, capsys):
    (tmp_path / "mybps.py").write_text(
        "from bpkit import Blueprint\nclass Box(Blueprint):\n    packages = ['vim']\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    assert main(["render", "mybps:Box"]) == 0
    assert 'name = "vim"' in capsys.readouterr().out


def test_list(capsys):
    assert main(["list", FLEET]) == 0
    out = capsys.readouterr().out
    assert "Hardened" in out and "[abstract]" in out
    assert "StagingWeb  bases: WebServer  [-> staging-web.toml]" in out


def test_diff(capsys):
    assert main(["diff", f"{FLEET}:WebServer", f"{FLEET}:WebServer"]) == 0
    assert capsys.readouterr().out == ""
    assert main(["diff", f"{FLEET}:WebServer", f"{FLEET}:MinimalWeb"]) == 1
    out = capsys.readouterr().out
    assert '-    { name = "aide", version = "*" },' in out


def test_explain(capsys):
    assert main(["explain", f"{FLEET}:StagingWeb", "packages"]) == 0
    assert "htop          <- StagingWeb" in capsys.readouterr().out


def test_overlay_command(capsys):
    assert main(["overlay", str(EXAMPLES / "overlays/web")]) == 0
    data = tomllib.loads(capsys.readouterr().out)
    paths = [f["path"] for f in data["customizations"]["files"]]
    assert "/usr/local/bin/healthcheck" in paths


def test_errors(capsys, tmp_path):
    assert main(["render", f"{FLEET}:Nope"]) == 2
    assert "has no attribute" in capsys.readouterr().err
    assert main(["render", "no_such_module_xyz:Cls"]) == 2
    bad = tmp_path / "bad.py"
    bad.write_text("from bpkit import Blueprint\nclass B(Blueprint):\n    pakages = []\n")
    assert main(["render", str(bad)]) == 2
    assert "did you mean 'packages'" in capsys.readouterr().err
    # no tracebacks for an unknown explain field or a missing file
    assert main(["explain", f"{FLEET}:StagingWeb", "pakages"]) == 2
    assert "did you mean 'packages'" in capsys.readouterr().err
    assert main(["audit", str(FLEET), "--advisories", "--packages", str(tmp_path / "no")]) == 2
    assert "bpkit: error:" in capsys.readouterr().err


def test_debug_customize_via_env(capsys, monkeypatch):
    monkeypatch.setenv("BPKIT_EXAMPLE_DEBUG", "1")
    assert main(["render", f"{FLEET}:StagingWeb"]) == 0
    data = tomllib.loads(capsys.readouterr().out)
    assert "gdb" in [p["name"] for p in data["packages"]]
    assert "debug-shell" in data["customizations"]["services"]["enabled"]


@pytest.fixture(autouse=True)
def _no_debug_env(monkeypatch):
    monkeypatch.delenv("BPKIT_EXAMPLE_DEBUG", raising=False)
    yield
    os.environ.pop("BPKIT_EXAMPLE_DEBUG", None)
