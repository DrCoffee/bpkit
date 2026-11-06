"""bpkit import: TOML blueprints → Python classes."""

import tomllib
from pathlib import Path

import pytest

from bpkit.cli import load_module, main
from bpkit.importer import BlueprintImportError, import_blueprints, parse_blueprint

GOLDEN = Path(__file__).resolve().parent / "golden"
GOLDEN_FILES = sorted(GOLDEN.glob("*.toml"))


def load(path: Path) -> dict:
    return tomllib.loads(path.read_text())


def run_module(source: str, tmp_path: Path, name: str = "imported.py"):
    path = tmp_path / name
    path.write_text(source)
    return load_module(str(path))


def normalized(value):
    """Order-insensitive view of a blueprint dict (for factored imports)."""
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in value.items()}
    if isinstance(value, list):
        items = [normalized(v) for v in value]
        return sorted(items, key=repr)
    return value


@pytest.mark.parametrize("path", GOLDEN_FILES, ids=lambda p: p.stem)
def test_single_import_round_trips_exactly(path, tmp_path):
    original = load(path)
    module = run_module(import_blueprints([original]), tmp_path)
    (cls,) = [
        c
        for c in vars(module).values()
        if getattr(c, "name", None) == original["name"]
        or (isinstance(c, type) and c.__module__ == module.__name__)
    ]
    assert cls().to_dict() == original


def test_factored_import_round_trips(tmp_path):
    originals = [load(p) for p in GOLDEN_FILES]
    source = import_blueprints(originals, base="Fleet")
    module = run_module(source, tmp_path)
    assert module.Fleet.is_abstract()
    for original in originals:
        cls = next(
            c
            for c in vars(module).values()
            if isinstance(c, type)
            and c.__module__ == module.__name__
            and not c.is_abstract()
            and c().name == original["name"]
        )
        assert normalized(cls().to_dict()) == normalized(original)
    # shared settings really moved to the base
    assert 'packages = ["nginx"]' in source.split("class MinimalWeb")[0]
    assert "distro" not in source.split("class MinimalWeb")[1]


def test_overlay_dir(tmp_path):
    originals = [load(p) for p in GOLDEN_FILES]
    out = tmp_path / "pkg"
    out.mkdir()
    source = import_blueprints(
        originals, base="Fleet", overlay_dir=out / "overlays", module_dir=out
    )
    assert 'overlay = Overlay("overlays/fleet", directories="none")' in source
    assert "File(" not in source
    script = out / "overlays/fleet/usr/local/bin/healthcheck"
    assert script.read_text().startswith("#!/bin/sh")
    assert script.stat().st_mode & 0o777 == 0o755
    module = run_module(source, out)
    for original in originals:
        cls = next(
            c
            for c in vars(module).values()
            if isinstance(c, type)
            and c.__module__ == module.__name__
            and not c.is_abstract()
            and c().name == original["name"]
        )
        assert normalized(cls().to_dict()) == normalized(original)


def test_parse_maps_paths_and_extras():
    parsed = parse_blueprint(
        {
            "name": "x",
            "packages": [{"name": "vim", "version": "*"}],
            "customizations": {
                "kernel": {"name": "kernel-rt", "append": "quiet audit=1"},
                "timezone": {"timezone": "UTC"},
                "user": [{"name": "ops"}],
                "filesystem": [{"mountpoint": "/var", "size": 1024}],
                "openscap": {"profile_id": "cis"},
            },
        }
    )
    assert parsed.fields["kernel"] == {"name": "kernel-rt"}
    assert parsed.fields["kernel_append"] == ["quiet", "audit=1"]
    assert parsed.fields["timezone"] == "UTC"
    assert parsed.fields["users"] == [{"name": "ops"}]
    assert parsed.fields["filesystem"] == [{"mountpoint": "/var", "minsize": 1024}]
    assert parsed.fields["extra_customizations"] == {"openscap": {"profile_id": "cis"}}


def test_extras_round_trip(tmp_path):
    original = {
        "name": "scap",
        "version": "1.2.0",
        "customizations": {"openscap": {"profile_id": "cis"}, "hostname": "h"},
    }
    module = run_module(import_blueprints([original]), tmp_path)
    assert module.Scap().to_dict() == original


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ({"packages": []}, "no name"),
        ({"name": "x", "bogus": 1}, "unknown top-level"),
        (
            {"name": "x", "customizations": {"filesystem": [{"mountpoint": "var", "minsize": 1}]}},
            "filesystem",
        ),
    ],
)
def test_parse_errors(data, match):
    with pytest.raises(BlueprintImportError, match=match):
        parse_blueprint(data)


def test_base_needs_two(tmp_path):
    with pytest.raises(BlueprintImportError, match="at least two"):
        import_blueprints([{"name": "a"}], base="Base")


def test_cli_import(tmp_path, capsys):
    out = tmp_path / "bps.py"
    files = [str(p) for p in GOLDEN_FILES]
    assert main(["import", *files, "--base", "Fleet", "-o", str(out)]) == 0
    assert "class Fleet(Blueprint, abstract=True)" in out.read_text()
    assert main(["import", *files, "-o", str(out)]) == 2
    assert "exists" in capsys.readouterr().err
    assert main(["import", str(GOLDEN_FILES[0])]) == 0
    assert "class MinimalWeb(Blueprint)" in capsys.readouterr().out
    # the generated module renders through the normal CLI
    assert main(["render", f"{out}:WebServer"]) == 0
    assert 'name = "web-server"' in capsys.readouterr().out
