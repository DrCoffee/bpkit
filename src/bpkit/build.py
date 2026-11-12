"""Build images locally with the ``image-builder`` CLI.

::

    from bpkit.build import build, manifest

    build(WebServer(), "qcow2", output_dir="out/")   # sudo/root usually needed
    manifest(WebServer(), "qcow2")                   # osbuild manifest JSON, no build

The blueprint is written to a temporary TOML file and passed with
``--blueprint``. ``distro`` defaults to the blueprint's ``distro`` field, and
``image-builder`` falls back to the host distro if neither is set.

Local repositories and mirrors
------------------------------

``repos`` replaces the distro's standard repositories for the build
(``--force-repo``); ``extra_repos`` adds to them (``--extra-repo``). Both take
base URLs (``https://``, ``http://`` or ``file://``) or a local directory of
RPMs, which is indexed with ``createrepo_c`` and passed as ``file://``. They are
used only to fetch packages: they are not written into the image (use the
``repositories`` field for that). Set them once for a hierarchy with class
attributes; relative directories are resolved against the declaring module::

    class AirGapped(Blueprint):
        BUILD_REPOS = [
            "http://mirror.lan/fedora/42/Everything/x86_64/os/",
            "http://mirror.lan/fedora/updates/42/Everything/x86_64/",
        ]

Subclasses inherit them like any class attribute; ``repos=`` / ``bpkit build
--repo`` override them. ``data_dir`` passes ``--data-dir`` for full control: a directory
holding ``<distro>.json`` repository files (``fedora-42.json``). Put them directly
in it: image-builder 17 doesn't look in a ``repositories/`` subdirectory.

Pre-build steps and spec files
------------------------------

``PRE_BUILD`` lists commands to run before ``image-builder`` (a string runs in
a shell, a list is an argv), and ``RPM_SPECS`` lists spec files to build with
``rpmbuild`` into a temporary repo that is added as an extra repo. Both run in
the directory of the module that declares them, ``PRE_BUILD`` first::

    class App(Blueprint):
        packages = ["myapp"]                    # still say what to install
        PRE_BUILD = ["make -C rpms sources"]    # e.g. create Source0 tarballs
        RPM_SPECS = ["rpms/myapp.spec"]         # sources are looked up next to the spec

They run for ``build`` and ``manifest`` (so ``bpkit sbom`` sees the packages),
after the blueprint is rendered: they can't change overlay contents.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .composer import blueprint_toml

if TYPE_CHECKING:
    from .blueprint import Blueprint

__all__ = ["BuildError", "build", "find_image_builder", "image_builder_command", "manifest"]


class BuildError(RuntimeError):
    """``image-builder`` is missing or exited non-zero."""

    def __init__(self, message: str, *, returncode: int | None = None, stderr: str = "") -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


def find_image_builder(executable: str | None = None) -> str:
    exe = executable or os.environ.get("BPKIT_IMAGE_BUILDER", "image-builder")
    path = shutil.which(exe)
    if path is None:
        raise BuildError(
            f"{exe!r} not found on PATH. Install it (e.g. `dnf install image-builder`) "
            "or set BPKIT_IMAGE_BUILDER."
        )
    return path


def image_builder_command(
    subcommand: str,
    image_type: str,
    blueprint_path: str | Path,
    *,
    distro: str | None = None,
    arch: str | None = None,
    output_dir: str | Path | None = None,
    extra_args: Sequence[str] = (),
    executable: str = "image-builder",
    repos: Sequence[str] = (),
    extra_repos: Sequence[str] = (),
    data_dir: str | Path | None = None,
) -> list[str]:
    """The argv for ``image-builder <subcommand> <type> --blueprint …``."""
    cmd = [executable, subcommand, image_type, "--blueprint", str(blueprint_path)]
    for url in repos:
        cmd += ["--force-repo", url]
    for url in extra_repos:
        cmd += ["--extra-repo", url]
    if data_dir is not None:
        cmd += ["--data-dir", str(data_dir)]
    if distro:
        cmd += ["--distro", distro]
    if arch:
        cmd += ["--arch", arch]
    if output_dir is not None:
        cmd += ["--output-dir", str(output_dir)]
    return cmd + list(extra_args)


def _distro_of(bp: Blueprint | Any, distro: str | None) -> str | None:
    return distro or getattr(bp, "distro", None)


def _declared(bp: Blueprint | Any, attr: str) -> tuple[Any, Path]:
    """A class attribute and the directory of the module that declares it."""
    from .blueprint import _module_dir

    if attr in getattr(bp, "__dict__", {}):  # set on the instance
        return vars(bp)[attr], Path.cwd()
    for klass in type(bp).__mro__:
        if attr in vars(klass):
            return vars(klass)[attr], _module_dir(klass.__module__) or Path.cwd()
    return (), Path.cwd()


def _listed(value: Any) -> list[Any]:
    """A bare string or path is one entry, not a sequence of characters."""
    return [value] if isinstance(value, (str, os.PathLike)) else list(value)


def _run(cmd: str | Sequence[Any], cwd: Path) -> None:
    """Run a pre-build step. Output is captured so stdout stays clean for ``bpkit sbom``."""
    shown = cmd if isinstance(cmd, str) else shlex.join(map(str, cmd))
    print(f"bpkit: {shown}", file=sys.stderr)
    try:
        result = subprocess.run(
            cmd, cwd=cwd, shell=isinstance(cmd, str), capture_output=True, text=True, check=False
        )
    except OSError as e:
        raise BuildError(f"can't run `{shown}`: {e}") from None
    if result.returncode != 0:
        raise BuildError(
            f"`{shown}` failed (exit {result.returncode}):\n"
            f"{(result.stdout + result.stderr).strip()[-2000:]}",
            returncode=result.returncode,
            stderr=result.stderr,
        )


def _repo_url(repo: str | os.PathLike[str], base: Path) -> str:
    """URLs pass through; a path is a local directory of RPMs, indexed and passed as file://."""
    if "://" in str(repo):
        return str(repo)
    path = (base / repo).resolve()
    _run(["createrepo_c", "--update", "--quiet", path], path)
    return path.as_uri()


def _prepare(bp: Blueprint | Any, tmp: Path) -> list[str]:
    """Run ``PRE_BUILD`` commands, then build ``RPM_SPECS`` into a repo under ``tmp``."""
    commands, cwd = _declared(bp, "PRE_BUILD")
    for cmd in _listed(commands):
        _run(cmd, cwd)
    specs, base = _declared(bp, "RPM_SPECS")
    if not specs:
        return []
    top = tmp / "rpmbuild"
    for spec in _listed(specs):
        spec = (base / spec).resolve()
        # ponytail: host rpmbuild, so packages target the host's distro and need its
        # BuildRequires installed. Fine for noarch/config packages; for anything else run
        # mock in PRE_BUILD and list its result directory in BUILD_EXTRA_REPOS.
        _run(
            ["rpmbuild", "-bb", "-D", f"_topdir {top}", "-D", f"_sourcedir {spec.parent}", spec],
            spec.parent,
        )
    _run(["createrepo_c", "--quiet", top / "RPMS"], tmp)
    return [(top / "RPMS").as_uri()]


def _repo_args(
    bp: Blueprint | Any,
    repos: Sequence[str] | None,
    extra_repos: Sequence[str] | None,
    data_dir: str | Path | None,
    built: Sequence[str] = (),
) -> dict[str, Any]:
    """Explicit arguments win over the blueprint's BUILD_REPOS / BUILD_EXTRA_REPOS.

    ``built`` (the repo ``_prepare`` made from ``RPM_SPECS``) is always added.
    """
    if data_dir is not None and not Path(data_dir).is_dir():
        # image-builder silently falls back to the default repos
        raise BuildError(f"data dir {data_dir} is not a directory")
    out: dict[str, Any] = {"data_dir": data_dir}
    for key, attr, given in (
        ("repos", "BUILD_REPOS", repos),
        ("extra_repos", "BUILD_EXTRA_REPOS", extra_repos),
    ):
        value, base = _declared(bp, attr) if given is None else (given, Path.cwd())
        out[key] = [_repo_url(r, base) for r in _listed(value)]
    out["extra_repos"] += built
    return out


def build(
    blueprint: Blueprint | Any,
    image_type: str,
    *,
    distro: str | None = None,
    arch: str | None = None,
    output_dir: str | Path = ".",
    extra_args: Sequence[str] = (),
    executable: str | None = None,
    capture: bool = False,
    repos: Sequence[str] | None = None,
    extra_repos: Sequence[str] | None = None,
    data_dir: str | Path | None = None,
) -> Path:
    """Run ``image-builder build`` and return the output directory.

    Output streams to the terminal unless ``capture=True``. Raises
    :class:`BuildError` if the build fails.
    """
    exe = find_image_builder(executable)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    _, text = blueprint_toml(blueprint)
    with tempfile.TemporaryDirectory(prefix="bpkit-") as tmp:
        bp_path = Path(tmp) / "blueprint.toml"
        bp_path.write_text(text, encoding="utf-8")
        cmd = image_builder_command(
            "build",
            image_type,
            bp_path,
            distro=_distro_of(blueprint, distro),
            arch=arch,
            output_dir=out,
            extra_args=extra_args,
            executable=exe,
            **_repo_args(blueprint, repos, extra_repos, data_dir, _prepare(blueprint, Path(tmp))),
        )
        result = subprocess.run(cmd, capture_output=capture, text=True, check=False)
    if result.returncode != 0:
        raise BuildError(
            f"image-builder build {image_type} failed (exit {result.returncode})",
            returncode=result.returncode,
            stderr=result.stderr or "",
        )
    return out


def manifest(
    blueprint: Blueprint | Any,
    image_type: str,
    *,
    distro: str | None = None,
    arch: str | None = None,
    extra_args: Sequence[str] = (),
    executable: str | None = None,
    repos: Sequence[str] | None = None,
    extra_repos: Sequence[str] | None = None,
    data_dir: str | Path | None = None,
) -> str:
    """Run ``image-builder manifest`` and return the osbuild manifest (JSON text)."""
    exe = find_image_builder(executable)
    _, text = blueprint_toml(blueprint)
    with tempfile.TemporaryDirectory(prefix="bpkit-") as tmp:
        bp_path = Path(tmp) / "blueprint.toml"
        bp_path.write_text(text, encoding="utf-8")
        cmd = image_builder_command(
            "manifest",
            image_type,
            bp_path,
            distro=_distro_of(blueprint, distro),
            arch=arch,
            extra_args=extra_args,
            executable=exe,
            **_repo_args(blueprint, repos, extra_repos, data_dir, _prepare(blueprint, Path(tmp))),
        )
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise BuildError(
            f"image-builder manifest {image_type} failed (exit {result.returncode}): "
            f"{result.stderr.strip()[-2000:]}",
            returncode=result.returncode,
            stderr=result.stderr,
        )
    return result.stdout
