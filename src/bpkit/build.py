"""Build images locally with the ``image-builder`` CLI.

::

    from bpkit.build import build, manifest

    build(WebServer(), "qcow2", output_dir="out/")   # sudo/root usually needed
    manifest(WebServer(), "qcow2")                   # osbuild manifest JSON, no build

The blueprint is written to a temporary TOML file and passed with
``--blueprint``. ``distro`` defaults to the blueprint's ``distro`` field, and
``image-builder`` falls back to the host distro if neither is set.
"""

from __future__ import annotations

import os
import shutil
import subprocess
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
) -> list[str]:
    """The argv for ``image-builder <subcommand> <type> --blueprint …``."""
    cmd = [executable, subcommand, image_type, "--blueprint", str(blueprint_path)]
    if distro:
        cmd += ["--distro", distro]
    if arch:
        cmd += ["--arch", arch]
    if output_dir is not None:
        cmd += ["--output-dir", str(output_dir)]
    return cmd + list(extra_args)


def _distro_of(bp: Blueprint | Any, distro: str | None) -> str | None:
    return distro or getattr(bp, "distro", None)


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
