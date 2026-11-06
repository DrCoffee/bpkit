"""Overlay import: turn a directory tree into ``files`` and ``directories``.

Lay out config files the way they should land on the image::

    overlay/
      etc/
        nginx/conf.d/site.conf
        myapp/
          .gitkeep            # marks an empty directory (git can't store those)
      usr/local/bin/healthcheck   # mode 0755 is kept

then declare ``overlay = Overlay("overlay")`` on a blueprint class. Every
regular file becomes a :class:`~bpkit.models.File` (contents inline, mode
taken from disk) and every leaf directory becomes a
:class:`~bpkit.models.Directory` with ``ensure_parents = true``.

Leaf directories are emitted with no mode, user or group unless you ask for
them. image-builder then creates them with ``exist_ok``, so a directory that
the distro already ships (``/etc/nginx``) is not a conflict. Overlay entries
merge by path like any other ``files`` / ``directories`` entry: a child
class's overlay file replaces its parent's, and ``Remove("/etc/x")`` drops one.
"""

from __future__ import annotations

import fnmatch
import os
import posixpath
import stat
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from .models import Directory, File

__all__ = ["DEFAULT_EXCLUDE", "Overlay", "OverlayError", "check_path_policy"]


class OverlayError(ValueError):
    """The overlay tree can't be turned into blueprint customizations."""


#: Names ignored by default. ``.gitkeep`` / ``.keep`` still mark their
#: directory as wanted, so empty directories survive in git.
DEFAULT_EXCLUDE: tuple[str, ...] = (
    ".gitkeep",
    ".keep",
    ".git",
    ".DS_Store",
    "*.swp",
    "*~",
    "*.orig",
    "*.rej",
)

#: Directories that always exist on an image; never emitted as Directory
#: entries (they'd be no-ops, and some are denied by image-builder's policy).
_ALWAYS_EXIST = frozenset(
    {
        "/",
        "/etc",
        "/opt",
        "/root",
        "/srv",
        "/usr",
        "/usr/local",
        "/usr/local/bin",
        "/usr/local/etc",
        "/usr/local/lib",
        "/usr/local/sbin",
        "/usr/local/share",
        "/var",
    }
)

# image-builder's default (non-ostree) path policies for file and directory
# customizations, from osbuild/images pkg/policies.
_DENIED_PREFIXES = (
    "/bin",
    "/boot",
    "/dev",
    "/efi",
    "/lib",
    "/lib64",
    "/lost+found",
    "/proc",
    "/run",
    "/sbin",
    "/sys",
    "/sysroot",
    "/tmp",
    "/usr",
    "/var/run",
    "/var/tmp",
)
_ALLOWED_UNDER_DENIED = ("/usr/local",)
_DENIED_FILES = frozenset({"/etc/fstab", "/etc/group", "/etc/passwd", "/etc/shadow"})


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def check_path_policy(path: str, *, is_file: bool) -> str | None:
    """Return why image-builder would reject ``path``, or ``None`` if it's allowed."""
    if is_file and path in _DENIED_FILES:
        return f"{path} is managed by image-builder (use users/user_groups customizations)"
    if any(_under(path, p) for p in _ALLOWED_UNDER_DENIED):
        return None
    for prefix in _DENIED_PREFIXES:
        if _under(path, prefix):
            return f"paths under {prefix} are not allowed by image-builder"
    return None


class Overlay:
    """A directory tree to copy onto the image as files and directories.

    :param root: the overlay directory. Relative paths are resolved against
        the directory of the module that defines the blueprint class (or the
        current directory for constructor kwargs).
    :param prefix: where ``root`` maps to on the image (default ``/``).
        ``Overlay("nginx-conf", prefix="/etc/nginx")`` works too.
    :param user, group: owner for every generated entry (default: unset,
        which image-builder treats as root).
    :param file_mode: force this mode on every file. By default each file's
        mode is taken from disk (``preserve_mode=True``).
    :param dir_mode: mode for generated directories. Default unset, which
        lets image-builder tolerate directories that already exist.
    :param directories: ``"leaves"`` (default) emits only directories that
        have no subdirectories, with ``ensure_parents`` so their parents get
        created too. ``"all"`` emits every directory. ``"none"`` emits none.
    :param exclude: glob patterns matched against each name and each
        relative path. Defaults to :data:`DEFAULT_EXCLUDE`.
    :param check_policy: reject paths image-builder's default policy refuses
        (``/usr``, ``/boot``, ``/etc/passwd`` …), so mistakes fail on import.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        prefix: str = "/",
        user: str | int | None = None,
        group: str | int | None = None,
        file_mode: str | int | None = None,
        dir_mode: str | int | None = None,
        preserve_mode: bool = True,
        directories: Literal["leaves", "all", "none"] = "leaves",
        exclude: Iterable[str] = DEFAULT_EXCLUDE,
        check_policy: bool = True,
    ) -> None:
        if not prefix.startswith("/"):
            raise OverlayError(f"prefix must be absolute, got {prefix!r}")
        if directories not in ("leaves", "all", "none"):
            raise OverlayError(
                f"directories must be 'leaves', 'all' or 'none', got {directories!r}"
            )
        self.root = Path(root)
        self.prefix = posixpath.normpath(prefix)
        self.user = user
        self.group = group
        self.file_mode = file_mode
        self.dir_mode = dir_mode
        self.preserve_mode = preserve_mode
        self.directories = directories
        self.exclude = tuple(exclude)
        self.check_policy = check_policy

    def __repr__(self) -> str:
        extra = "" if self.prefix == "/" else f", prefix={self.prefix!r}"
        return f"Overlay({str(self.root)!r}{extra})"

    def resolved(self, base_dir: str | os.PathLike[str] | None) -> Overlay:
        """A copy with ``root`` made absolute against ``base_dir``."""
        if self.root.is_absolute() or base_dir is None:
            root = self.root.resolve()
        else:
            root = (Path(base_dir) / self.root).resolve()
        clone = object.__new__(Overlay)
        clone.__dict__.update(self.__dict__)
        clone.root = root
        return clone

    def _excluded(self, name: str, rel: str) -> bool:
        return any(
            fnmatch.fnmatchcase(name, p) or fnmatch.fnmatchcase(rel, p) for p in self.exclude
        )

    def _target(self, rel: str) -> str:
        return self.prefix if rel in ("", ".") else posixpath.join(self.prefix, rel)

    def entries(self) -> tuple[list[Directory], list[File]]:
        """Walk the tree and return ``(directories, files)`` in sorted order."""
        root = self.root
        if not root.is_dir():
            raise OverlayError(f"overlay root {str(root)!r} is not a directory")

        dirs: list[Directory] = []
        files: list[File] = []
        problems: list[str] = []

        for current, dirnames, filenames in os.walk(root, followlinks=False):
            rel_dir = Path(current).relative_to(root).as_posix()
            rel_dir = "" if rel_dir == "." else rel_dir
            dirnames[:] = sorted(
                d for d in dirnames if not self._excluded(d, posixpath.join(rel_dir, d))
            )
            # Symlinked directories show up in dirnames; reject them.
            for d in list(dirnames):
                if (Path(current) / d).is_symlink():
                    problems.append(f"{posixpath.join(rel_dir, d)}: symlinks are not supported")
                    dirnames.remove(d)

            target_dir = self._target(rel_dir)
            emit_dir = self.directories == "all" or (self.directories == "leaves" and not dirnames)
            if emit_dir and target_dir not in _ALWAYS_EXIST:
                if self.check_policy and (why := check_path_policy(target_dir, is_file=False)):
                    problems.append(f"{target_dir}: {why}")
                dirs.append(
                    Directory(
                        target_dir,
                        user=self.user,
                        group=self.group,
                        mode=self.dir_mode,
                        ensure_parents=True,
                    )
                )

            for name in sorted(filenames):
                rel = posixpath.join(rel_dir, name)
                if self._excluded(name, rel):
                    continue
                path = Path(current) / name
                target = self._target(rel)
                st = path.lstat()
                if stat.S_ISLNK(st.st_mode):
                    problems.append(f"{rel}: symlinks are not supported")
                    continue
                if not stat.S_ISREG(st.st_mode):
                    problems.append(f"{rel}: not a regular file")
                    continue
                if self.check_policy and (why := check_path_policy(target, is_file=True)):
                    problems.append(f"{target}: {why}")
                    continue
                try:
                    data = path.read_bytes().decode("utf-8")
                except UnicodeDecodeError:
                    problems.append(
                        f"{rel}: not UTF-8 text (blueprint files hold text; "
                        "exclude it or add it as File(path, uri=...))"
                    )
                    continue
                if self.file_mode is not None:
                    mode: str | int | None = self.file_mode
                elif self.preserve_mode:
                    mode = stat.S_IMODE(st.st_mode)
                else:
                    mode = None
                files.append(File(target, data, user=self.user, group=self.group, mode=mode))

        if problems:
            raise OverlayError(
                f"overlay {str(root)!r} has entries image-builder can't use:\n  "
                + "\n  ".join(problems)
            )
        return dirs, files
