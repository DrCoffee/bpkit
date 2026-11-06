"""Overlay import: directory trees → files/directories customizations."""

import os
import re
import textwrap
from pathlib import Path

import pytest

from bpkit import Blueprint, BlueprintError, File, Overlay, OverlayError, Remove, Replace, Unset


def make_tree(root: Path, spec: dict[str, str | None]) -> Path:
    """``{"etc/app/x.conf": "data", "etc/empty/": None}`` → files on disk."""
    for rel, data in spec.items():
        p = root / rel
        if rel.endswith("/"):
            p.mkdir(parents=True, exist_ok=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(data or "")
            p.chmod(0o644)
    return root


def test_entries(tmp_path):
    root = make_tree(
        tmp_path / "ov",
        {
            "etc/nginx/conf.d/site.conf": "server {}\n",
            "etc/myapp/.gitkeep": "",
            "etc/hosts.allow": "ALL: LOCAL\n",
            "usr/local/bin/tool": "#!/bin/sh\n",
            "var/lib/app/": None,
        },
    )
    (root / "usr/local/bin/tool").chmod(0o755)
    dirs, files = Overlay(root).entries()
    assert [d.path for d in dirs] == ["/etc/myapp", "/etc/nginx/conf.d", "/var/lib/app"]
    assert all(d.ensure_parents and d.mode is None for d in dirs)
    assert [(f.path, f.mode) for f in files] == [
        ("/etc/hosts.allow", "0644"),
        ("/etc/nginx/conf.d/site.conf", "0644"),
        ("/usr/local/bin/tool", "0755"),
    ]
    assert files[1].data == "server {}\n"


def test_prefix_and_options(tmp_path):
    root = make_tree(tmp_path / "conf", {"conf.d/a.conf": "a", "nginx.conf": "n"})
    dirs, files = Overlay(
        root,
        prefix="/etc/nginx",
        user="nginx",
        group="nginx",
        file_mode=0o600,
        dir_mode="0750",
        directories="all",
    ).entries()
    assert [d.path for d in dirs] == ["/etc/nginx", "/etc/nginx/conf.d"]
    assert {d.mode for d in dirs} == {"0750"}
    assert [(f.path, f.mode, f.user) for f in files] == [
        ("/etc/nginx/nginx.conf", "0600", "nginx"),
        ("/etc/nginx/conf.d/a.conf", "0600", "nginx"),
    ]
    _, files = Overlay(root, prefix="/etc/nginx", preserve_mode=False).entries()
    assert {f.mode for f in files} == {None}
    dirs, _ = Overlay(root, prefix="/etc/nginx", directories="none").entries()
    assert dirs == []


def test_exclude(tmp_path):
    root = make_tree(
        tmp_path / "ov",
        {"etc/a.conf": "a", "etc/a.conf~": "x", "etc/.a.conf.swp": "x", "etc/skip/b": "b"},
    )
    _, files = Overlay(root).entries()
    assert [f.path for f in files] == ["/etc/a.conf", "/etc/skip/b"]
    dirs, files = Overlay(root, exclude=["skip", "*~", "*.swp"]).entries()
    assert [f.path for f in files] == ["/etc/a.conf"]
    assert dirs == []  # /etc always exists


@pytest.mark.parametrize(
    "rel", ["usr/lib/systemd/system/x.service", "etc/passwd", "boot/grub2/x", "tmp/x"]
)
def test_policy_rejects(tmp_path, rel):
    root = make_tree(tmp_path / "ov", {rel: "x"})
    with pytest.raises(OverlayError, match=r"not allowed|managed by image-builder"):
        Overlay(root).entries()
    Overlay(root, check_policy=False).entries()


def test_symlinks_and_binary_rejected(tmp_path):
    root = make_tree(tmp_path / "ov", {"etc/a": "a"})
    os.symlink("a", root / "etc/link")
    (root / "etc/bin").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(OverlayError) as exc:
        Overlay(root).entries()
    msg = str(exc.value)
    assert "etc/link: symlinks are not supported" in msg
    assert "etc/bin: not UTF-8" in msg


def test_missing_root(tmp_path):
    with pytest.raises(OverlayError, match="not a directory"):
        Overlay(tmp_path / "nope").entries()


def test_blueprint_overlay_inheritance(tmp_path, monkeypatch):
    make_tree(tmp_path / "base", {"etc/motd": "base\n", "etc/app/a.conf": "a\n"})
    make_tree(tmp_path / "web", {"etc/motd": "web\n", "etc/nginx/n.conf": "n\n"})
    monkeypatch.chdir(tmp_path)

    class Base(Blueprint):
        overlay = Overlay(tmp_path / "base")

    class Web(Base):
        overlay = [tmp_path / "web"]  # plain paths work too
        files = [File("/etc/app/a.conf", "explicit\n")]

    class Lean(Web):
        files = [Remove("/etc/motd")]

    web = Web()
    assert {f.path: f.data for f in web.files} == {
        "/etc/motd": "web\n",  # child overlay replaces parent's file
        "/etc/app/a.conf": "explicit\n",  # explicit entry in the same class wins
        "/etc/nginx/n.conf": "n\n",
    }
    assert [d.path for d in web.directories] == ["/etc/app", "/etc/nginx"]
    assert "/etc/motd" not in Lean().files
    assert "files: \n" not in Web.explain("files")
    assert "/etc/motd          <- Web" in Web.explain("files")


def test_overlay_relative_to_defining_module(tmp_path):
    make_tree(tmp_path / "pkg/overlay", {"etc/x.conf": "x"})
    (tmp_path / "pkg/bps.py").write_text(
        textwrap.dedent(
            """
            from bpkit import Blueprint, Overlay

            class Box(Blueprint):
                overlay = Overlay("overlay")
            """
        )
    )
    from bpkit.cli import load_module

    module = load_module(str(tmp_path / "pkg/bps.py"))
    assert [f.path for f in module.Box().files] == ["/etc/x.conf"]


def test_overlay_errors_surface_at_class_definition(tmp_path):
    make_tree(tmp_path / "bad", {"usr/bin/x": "x"})
    with pytest.raises(OverlayError):

        class Bad(Blueprint):
            overlay = Overlay(tmp_path / "bad")

    with pytest.raises(BlueprintError, match="expected Overlay"):

        class Bad2(Blueprint):
            overlay = 42


def test_overlay_kwarg(tmp_path, monkeypatch):
    make_tree(tmp_path / "ov", {"etc/k": "k"})
    monkeypatch.chdir(tmp_path)

    class A(Blueprint):
        pass

    assert [f.path for f in A(overlay="ov").files] == ["/etc/k"]


# -- dropping inherited overlays ---------------------------------------------


@pytest.fixture
def layered(tmp_path):
    make_tree(tmp_path / "base", {"etc/motd": "base\n", "etc/app/a.conf": "a\n"})
    make_tree(tmp_path / "web", {"etc/motd": "web\n", "etc/app/w.conf": "w\n"})

    class Base(Blueprint):
        files = [File("/etc/issue", "explicit issue\n")]
        overlay = Overlay(tmp_path / "base")

    class Web(Base):
        overlay = Overlay(tmp_path / "web")

    return tmp_path, Base, Web


def contents(bp):
    return {f.path: f.data for f in bp.files}


def test_remove_overlay_reveals_what_it_covered(layered):
    tmp, Base, Web = layered

    class NoWeb(Web):
        overlay = [Remove(tmp / "web")]

    bp = NoWeb()
    assert contents(bp) == {
        "/etc/issue": "explicit issue\n",
        "/etc/motd": "base\n",  # base's version comes back
        "/etc/app/a.conf": "a\n",
    }
    assert [d.path for d in bp.directories] == ["/etc/app"]  # still needed by base
    assert re.search(r"/etc/motd +<- Base", NoWeb.explain("files"))


def test_remove_lower_overlay_keeps_upper(layered):
    tmp, Base, Web = layered

    class NoBase(Web):
        overlay = [Remove(tmp / "base")]

    assert contents(NoBase()) == {
        "/etc/issue": "explicit issue\n",
        "/etc/motd": "web\n",
        "/etc/app/w.conf": "w\n",
    }
    assert [d.path for d in NoBase().directories] == ["/etc/app"]


def test_remove_overlay_by_trailing_path(layered):
    _, _, Web = layered

    class NoWeb(Web):
        overlay = [Remove("web")]  # resolved relative to *this* module; falls back to suffix

    assert "/etc/app/w.conf" not in NoWeb().files


def test_unset_and_replace_drop_all_overlays(layered, tmp_path):
    tmp, _, Web = layered
    make_tree(tmp / "other", {"etc/other": "o\n"})

    class Bare(Web):
        overlay = Unset

    class Other(Web):
        overlay = Replace(Overlay(tmp / "other"))

    assert contents(Bare()) == {"/etc/issue": "explicit issue\n"}  # explicit files stay
    assert Bare().directories.keys() == []
    assert contents(Other()) == {"/etc/issue": "explicit issue\n", "/etc/other": "o\n"}


def test_explicit_file_over_overlay_survives_drop(layered):
    tmp, _, Web = layered

    class Pinned(Web):
        files = [File("/etc/motd", "pinned\n")]

    class Dropped(Pinned):
        overlay = Unset

    assert contents(Dropped())["/etc/motd"] == "pinned\n"


def test_explicit_lower_file_restored(tmp_path):
    make_tree(tmp_path / "ov", {"etc/issue": "overlay\n"})

    class A(Blueprint):
        files = [File("/etc/issue", "explicit\n")]

    class B(A):
        overlay = Overlay(tmp_path / "ov")

    class C(B):
        overlay = [Remove(tmp_path / "ov")]

    assert contents(B())["/etc/issue"] == "overlay\n"
    assert contents(C())["/etc/issue"] == "explicit\n"


def test_remove_unknown_overlay_raises(layered):
    tmp, _, Web = layered
    with pytest.raises(BlueprintError, match="no inherited overlay"):

        class Typo(Web):
            overlay = [Remove(tmp / "wbe")]

    class Mixin(Blueprint, abstract=True):  # abstract: checked when used
        overlay = [Remove("web")]

    class Ok(Mixin, Web):
        pass

    assert "/etc/app/w.conf" not in Ok().files


def test_drop_overlay_via_kwargs(layered):
    _, _, Web = layered
    assert contents(Web(overlay=Unset)) == {"/etc/issue": "explicit issue\n"}
