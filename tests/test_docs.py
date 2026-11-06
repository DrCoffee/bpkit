"""Run every ```python block in docs/guide.md, in order, so the guide can't rot."""

import re
from pathlib import Path

import pytest

GUIDE = Path(__file__).resolve().parent.parent / "docs" / "guide.md"
TEXT = GUIDE.read_text()
BLOCKS = re.findall(r"```python\n(.*?)```", TEXT, flags=re.S)

TREES = {
    "overlays/web/etc/nginx/conf.d/site.conf": "server {}\n",
    "overlays/web/etc/myapp/.gitkeep": "",
    "overlays/web/usr/local/bin/healthcheck": "#!/bin/sh\n",
    "overlays/debug/etc/profile.d/debug.sh": "set -x\n",
    "overlays/other/etc/other.conf": "x\n",
}


@pytest.fixture
def guide_ns(tmp_path, monkeypatch):
    """Execute the guide's blocks in a scratch dir holding its overlay trees."""
    for rel, data in TREES.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("IMAGE_DEBUG", raising=False)
    ns: dict = {"__name__": "bpkit_guide"}
    for i, block in enumerate(BLOCKS, 1):
        try:
            exec(compile(block, f"{GUIDE.name}[block {i}]", "exec"), ns)
        except Exception as e:  # pragma: no cover - shows which block broke
            pytest.fail(f"guide block {i} failed: {e!r}\n{block}")
    return ns


def test_guide_examples_run(capsys, guide_ns):
    assert len(BLOCKS) >= 8
    assert 'name = "base"' in capsys.readouterr().out


def test_guide_explain_sample_matches(guide_ns):
    sample = re.search(r"```\n(packages:\n.*?)```", TEXT, flags=re.S).group(1)

    def norm(s: str) -> list[str]:
        return [re.sub(r"\s+", " ", line.strip()) for line in s.strip().splitlines()]

    assert norm(guide_ns["App"]().explain("packages")) == norm(sample)
