"""Golden-file tests for realistic blueprints.

Regenerate with ``BPKIT_UPDATE_GOLDEN=1 pytest tests/test_golden.py`` and
review the diff.
"""

import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from bpkit.cli import load_module, module_blueprints

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / "golden"
FLEET = load_module(str(HERE.parent / "examples" / "fleet.py"))
CLASSES = module_blueprints(FLEET)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("BPKIT_EXAMPLE_DEBUG", raising=False)


@pytest.mark.parametrize("cls", CLASSES, ids=lambda c: c.__name__)
def test_golden(cls):
    bp = cls()
    text = bp.to_toml()
    path = GOLDEN / f"{bp.name}.toml"
    if os.environ.get("BPKIT_UPDATE_GOLDEN"):
        GOLDEN.mkdir(exist_ok=True)
        path.write_text(text)
    assert path.exists(), f"missing golden file {path}; run with BPKIT_UPDATE_GOLDEN=1"
    assert text == path.read_text()
    assert tomllib.loads(text) == bp.to_dict()


@pytest.mark.skipif(shutil.which("image-builder") is None, reason="image-builder not on PATH")
@pytest.mark.parametrize("cls", CLASSES, ids=lambda c: c.__name__)
def test_image_builder_accepts(cls, tmp_path):
    """image-builder can build a manifest from the rendered blueprint."""
    bp = cls(distro=os.environ.get("BPKIT_TEST_DISTRO", "fedora-42"))
    # The example's internal repo and container don't exist; keep the rest.
    bp.repositories.clear()
    bp.containers.clear()
    path = bp.write(tmp_path / "bp.toml")
    result = subprocess.run(
        ["image-builder", "manifest", "qcow2", "--blueprint", str(path)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr
