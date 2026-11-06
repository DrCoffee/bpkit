"""``bpkit`` command-line interface."""

from __future__ import annotations

import argparse
import difflib
import importlib
import importlib.util
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from .blueprint import Blueprint, iter_blueprints
from .overlay import Overlay
from .render import to_toml

__all__ = ["main"]


class CLIError(Exception):
    pass


def load_module(spec: str) -> ModuleType:
    """Import ``pkg.module`` or a ``path/to/file.py``."""
    if spec.endswith(".py") or os.sep in spec or "/" in spec:
        path = Path(spec).resolve()
        if not path.is_file():
            raise CLIError(f"no such file: {spec}")
        name = f"_bpkit_{path.stem}_{abs(hash(str(path)))}"
        mspec = importlib.util.spec_from_file_location(name, path)
        if mspec is None or mspec.loader is None:  # pragma: no cover
            raise CLIError(f"can't import {spec}")
        module = importlib.util.module_from_spec(mspec)
        sys.modules[name] = module
        sys.path.insert(0, str(path.parent))
        try:
            mspec.loader.exec_module(module)
        finally:
            sys.path.remove(str(path.parent))
        return module
    if "" not in sys.path and os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())
    try:
        return importlib.import_module(spec)
    except ModuleNotFoundError as e:
        raise CLIError(f"can't import module {spec!r}: {e}") from None


def split_target(target: str) -> tuple[str, str | None]:
    # "C:\\x.py:Cls" isn't a concern on Linux; split on the last colon.
    module, sep, attr = target.rpartition(":")
    if not sep or not module:
        return target, None
    return module, attr


def load_class(target: str) -> type[Blueprint]:
    module_spec, attr = split_target(target)
    if attr is None:
        raise CLIError(f"expected module:ClassName, got {target!r}")
    module = load_module(module_spec)
    obj = module
    for part in attr.split("."):
        try:
            obj = getattr(obj, part)
        except AttributeError:
            raise CLIError(f"{module_spec!r} has no attribute {attr!r}") from None
    if not (isinstance(obj, type) and issubclass(obj, Blueprint)):
        raise CLIError(f"{target} is not a Blueprint subclass")
    return obj


def module_blueprints(
    module: ModuleType, *, include_abstract: bool = False
) -> list[type[Blueprint]]:
    """Blueprint classes defined in (not just imported into) ``module``."""
    classes = [c for c in iter_blueprints(vars(module).values()) if c.__module__ == module.__name__]
    if not include_abstract:
        classes = [c for c in classes if not c.is_abstract()]
    return classes


def render_class(cls: type[Blueprint]) -> str:
    return cls().to_toml()


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_render(args: argparse.Namespace) -> int:
    module_spec, attr = split_target(args.target)
    if attr is not None:
        text = render_class(load_class(args.target))
        if args.output in (None, "-"):
            sys.stdout.write(text)
        else:
            Path(args.output).write_text(text, encoding="utf-8")
            print(f"wrote {args.output}", file=sys.stderr)
        return 0

    module = load_module(module_spec)
    classes = module_blueprints(module)
    if not classes:
        raise CLIError(f"no concrete Blueprint classes in {module_spec}")
    outdir = Path(args.output or ".")
    outdir.mkdir(parents=True, exist_ok=True)
    rendered: dict[str, tuple[str, str]] = {}
    for cls in classes:
        bp = cls()
        if bp.name in rendered:
            raise CLIError(
                f"{cls.__qualname__} and {rendered[bp.name][0]} both render to {bp.name}.toml"
            )
        rendered[bp.name] = (cls.__qualname__, bp.to_toml())
    for name, (_, text) in rendered.items():
        path = outdir / f"{name}.toml"
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path}", file=sys.stderr)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    module = load_module(args.module)
    classes = module_blueprints(module, include_abstract=True)
    if not classes:
        print(f"no Blueprint classes in {args.module}", file=sys.stderr)
        return 1
    width = max(len(c.__qualname__) for c in classes)
    for cls in classes:
        bases = ", ".join(b.__qualname__ for b in cls.blueprint_bases()) or "-"
        tag = "abstract" if cls.is_abstract() else f"-> {cls._identity['name']}.toml"
        print(f"{cls.__qualname__.ljust(width)}  bases: {bases}  [{tag}]")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    a = render_class(load_class(args.a)).splitlines(keepends=True)
    b = render_class(load_class(args.b)).splitlines(keepends=True)
    diff = list(difflib.unified_diff(a, b, fromfile=args.a, tofile=args.b))
    sys.stdout.writelines(diff)
    return 1 if diff else 0


def cmd_explain(args: argparse.Namespace) -> int:
    cls = load_class(args.target)
    print(cls().explain(args.field))
    return 0


def cmd_overlay(args: argparse.Namespace) -> int:
    """Print the files/directories TOML an overlay directory expands to."""
    ov = Overlay(
        args.directory,
        prefix=args.prefix,
        directories=args.directories,
        preserve_mode=not args.no_mode,
        check_policy=not args.no_policy,
    ).resolved(Path.cwd())
    dirs, files = ov.entries()
    data: dict[str, object] = {"customizations": {}}
    custom = data["customizations"]
    assert isinstance(custom, dict)
    if dirs:
        custom["directories"] = [d.to_blueprint() for d in dirs]
    if files:
        custom["files"] = [f.to_blueprint() for f in files]
    sys.stdout.write(to_toml(data))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bpkit", description="Render Python-defined image-builder blueprints to TOML."
    )
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser(
        "render",
        help="render a blueprint class, or every concrete class in a module",
        description="TARGET is module:Class (prints TOML, or writes -o FILE) or a module "
        "(writes <name>.toml for each concrete class into -o DIR, default '.'). "
        "Modules can be dotted names or paths to .py files.",
    )
    r.add_argument("target")
    r.add_argument("-o", "--output", help="output file (class) or directory (module)")
    r.set_defaults(func=cmd_render)

    ls = sub.add_parser("list", help="list blueprint classes and their bases")
    ls.add_argument("module")
    ls.set_defaults(func=cmd_list)

    d = sub.add_parser("diff", help="diff the rendered TOML of two blueprints")
    d.add_argument("a", metavar="module:A")
    d.add_argument("b", metavar="module:B")
    d.set_defaults(func=cmd_diff)

    e = sub.add_parser("explain", help="show which class contributed each value")
    e.add_argument("target", metavar="module:Class")
    e.add_argument("field", nargs="?")
    e.set_defaults(func=cmd_explain)

    o = sub.add_parser(
        "overlay", help="print the files/directories customizations for an overlay directory"
    )
    o.add_argument("directory")
    o.add_argument("--prefix", default="/", help="where DIRECTORY maps to on the image")
    o.add_argument("--directories", choices=["leaves", "all", "none"], default="leaves")
    o.add_argument("--no-mode", action="store_true", help="don't copy file modes from disk")
    o.add_argument("--no-policy", action="store_true", help="skip image-builder path checks")
    o.set_defaults(func=cmd_overlay)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except CLIError as e:
        print(f"bpkit: error: {e}", file=sys.stderr)
        return 2
    except (TypeError, ValueError) as e:
        # Blueprint declaration errors surface here on import.
        print(f"bpkit: error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
