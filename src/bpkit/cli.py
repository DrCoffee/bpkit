"""``bpkit`` command-line interface."""

from __future__ import annotations

import argparse
import difflib
import importlib
import importlib.util
import json
import os
import sys
from collections.abc import Callable, Sequence
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


def cmd_import(args: argparse.Namespace) -> int:
    from .composer import DEFAULT_SOCKET, ComposerClient
    from .importer import import_blueprints, load_toml_files

    blueprints = load_toml_files(args.files)
    if args.from_composer:
        import tomllib

        client = ComposerClient(args.socket or DEFAULT_SOCKET)
        blueprints += [tomllib.loads(client.get_blueprint(n)) for n in args.from_composer]
    if not blueprints:
        raise CLIError("give TOML files and/or --from-composer NAME")
    out = Path(args.output) if args.output not in (None, "-") else None
    source = import_blueprints(
        blueprints,
        base=args.base,
        overlay_dir=args.overlay_dir,
        module_dir=out.parent if out else Path.cwd(),
        source="bpkit import "
        + " ".join(args.files + [f"--from-composer {n}" for n in args.from_composer]),
    )
    if out is None:
        sys.stdout.write(source)
    else:
        if out.exists() and not args.force:
            raise CLIError(f"{out} exists; pass --force to overwrite")
        out.write_text(source, encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    return 0


def _progress(prefix: str) -> Callable[[dict], None]:
    def show(status: dict) -> None:
        state = status.get("queue_status") or status.get("status")
        print(f"{prefix}: {state}", file=sys.stderr)

    return show


def _hosted_kwargs(args: argparse.Namespace) -> dict:
    kw: dict = {"lossy": args.lossy}
    if args.distro:
        kw["distribution"] = args.distro
    if args.arch:
        kw["architecture"] = args.arch
    return kw


def cmd_push(args: argparse.Namespace) -> int:
    bp = load_class(args.target)()
    if args.backend == "composer":
        from .composer import DEFAULT_SOCKET, ComposerClient

        name = ComposerClient(args.socket or DEFAULT_SOCKET).push(bp)
        print(f"pushed blueprint {name!r} to osbuild-composer")
    else:
        from .hosted import HostedClient

        if not args.type:
            raise CLIError("hosted blueprints need at least one --type")
        bp_id = HostedClient.from_env().push(bp, args.type, **_hosted_kwargs(args))
        print(bp_id)
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    bp = load_class(args.target)()
    outdir = Path(args.output)
    if args.backend == "local":
        from .build import build

        build(
            bp,
            args.type,
            distro=args.distro,
            arch=args.arch,
            output_dir=outdir,
            extra_args=args.extra,
        )
        print(f"built {args.type} into {outdir}", file=sys.stderr)
        return 0

    if args.backend == "composer":
        from .composer import DEFAULT_SOCKET, ComposerClient

        client = ComposerClient(args.socket or DEFAULT_SOCKET)
        name = client.push(bp)
        build_id = client.compose(name, args.type)
        print(build_id)
        if args.no_wait:
            return 0
        client.wait(build_id, poll=args.poll, on_status=_progress(build_id))
        if not args.no_download:
            outdir.mkdir(parents=True, exist_ok=True)
            print(f"downloaded {client.download(build_id, outdir)}", file=sys.stderr)
        return 0

    from .hosted import HostedClient

    hosted = HostedClient.from_env()
    compose_id = hosted.compose(bp, args.type, upload_type=args.upload_type, **_hosted_kwargs(args))
    print(compose_id)
    if args.no_wait:
        return 0
    status = hosted.wait(compose_id, poll=args.poll, on_status=_progress(compose_id))
    if args.no_download or args.upload_type != "aws.s3":
        print(json.dumps(status.get("upload_status", {}), indent=2))
        return 0
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"downloaded {hosted.download(compose_id, outdir)}", file=sys.stderr)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    if args.backend == "composer":
        from .composer import DEFAULT_SOCKET, ComposerClient

        status = ComposerClient(args.socket or DEFAULT_SOCKET).compose_status(args.id)
    else:
        from .hosted import HostedClient

        status = HostedClient.from_env().compose_status(args.id)
    print(json.dumps(status, indent=2))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    from .audit import (
        audit,
        check_advisories,
        load_package_list,
        pinned_packages,
        resolve_with_composer,
    )

    classes: list[type[Blueprint]] = []
    for target in args.targets:
        if split_target(target)[1] is not None:
            classes.append(load_class(target))
        else:
            found = module_blueprints(load_module(target))
            if not found:
                raise CLIError(f"no concrete Blueprint classes in {target}")
            classes.extend(found)

    listed = load_package_list(args.packages) if args.packages else None
    reports = []
    for cls in classes:
        bp = cls()
        report = audit(bp, hosted=args.hosted, ignore=args.ignore)
        if args.advisories:
            if listed is not None:
                packages = listed
            elif args.depsolve == "composer":
                from .composer import DEFAULT_SOCKET, ComposerClient

                packages = resolve_with_composer(bp, ComposerClient(args.socket or DEFAULT_SOCKET))
            else:
                packages = pinned_packages(bp)
            check_advisories(report, packages, args.distro or bp.distro)
            report.findings = [f for f in report.findings if f.code not in set(args.ignore)]
        reports.append(report)

    if args.json:
        print(json.dumps([r.to_dict() for r in reports], indent=2))
    else:
        print("\n\n".join(r.to_text(show_info=not args.quiet) for r in reports))
    return 1 if any(r.failed(strict=args.strict) for r in reports) else 0


def _add_remote_args(p: argparse.ArgumentParser, backends: list[str], default: str) -> None:
    p.add_argument(
        "--backend",
        choices=backends,
        default=default,
        help=f"where to build/push (default: {default})",
    )
    p.add_argument(
        "--socket", default=None, help="osbuild-composer socket (default /run/weldr/api.socket)"
    )
    p.add_argument(
        "--distro", help="distro override (hosted: required unless the blueprint sets one)"
    )
    p.add_argument("--arch", help="architecture (default: the blueprint's, else x86_64)")
    p.add_argument(
        "--lossy",
        action="store_true",
        help="hosted: drop settings the service can't express instead of failing",
    )


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

    im = sub.add_parser(
        "import",
        help="generate Python classes from existing TOML blueprints",
        description="Turn blueprint TOML into a bpkit module. With several blueprints, "
        "--base NAME factors what they share into an abstract base class.",
    )
    im.add_argument("files", nargs="*", help="blueprint TOML files")
    im.add_argument("-o", "--output", help="write the module here (default: stdout)")
    im.add_argument("--base", help="factor common settings into an abstract class of this name")
    im.add_argument("--overlay-dir", help="write inline file contents here and use Overlay(...)")
    im.add_argument(
        "--from-composer",
        action="append",
        default=[],
        metavar="NAME",
        help="also import a blueprint stored in osbuild-composer",
    )
    im.add_argument("--socket", default=None, help="osbuild-composer socket")
    im.add_argument("--force", action="store_true", help="overwrite -o if it exists")
    im.set_defaults(func=cmd_import)

    pu = sub.add_parser("push", help="upload a blueprint to osbuild-composer or the hosted service")
    pu.add_argument("target", metavar="module:Class")
    _add_remote_args(pu, ["composer", "hosted"], "composer")
    pu.add_argument(
        "--type",
        action="append",
        default=[],
        help="hosted: image type(s) to store with the blueprint",
    )
    pu.set_defaults(func=cmd_push)

    b = sub.add_parser(
        "build",
        help="build an image locally, via osbuild-composer, or on the hosted service",
        description="local runs `image-builder build` (extra image-builder args go after --); "
        "composer pushes the blueprint, "
        "composes, waits and downloads; hosted composes on console.redhat.com "
        "(credentials from RH_OFFLINE_TOKEN or RH_CLIENT_ID/RH_CLIENT_SECRET).",
    )
    b.add_argument("target", metavar="module:Class")
    b.add_argument("-t", "--type", required=True, help="image type, e.g. qcow2, ami, guest-image")
    b.add_argument("-o", "--output", default=".", help="output directory (default: .)")
    _add_remote_args(b, ["local", "composer", "hosted"], "local")
    b.add_argument("--upload-type", default="aws.s3", help="hosted upload target (default aws.s3)")
    b.add_argument("--no-wait", action="store_true", help="print the compose id and exit")
    b.add_argument("--no-download", action="store_true", help="don't download the result")
    b.add_argument("--poll", type=float, default=15, help="seconds between status checks")
    b.set_defaults(func=cmd_build)

    a = sub.add_parser(
        "audit",
        help="check blueprints for mistakes, risky settings and package advisories",
        description="Offline checks run by default. --advisories also looks up known "
        "advisories on OSV.dev, using exact versions from --depsolve composer, "
        "--packages FILE (rpm -qa output), or versions pinned in the blueprint. "
        "Exit status 1 if there are errors (or warnings with --strict).",
    )
    a.add_argument("targets", nargs="+", metavar="TARGET", help="module or module:Class")
    a.add_argument("--json", action="store_true", help="machine-readable output")
    a.add_argument("--strict", action="store_true", help="fail on warnings too")
    a.add_argument("-q", "--quiet", action="store_true", help="hide info findings")
    a.add_argument(
        "--ignore",
        action="append",
        default=[],
        metavar="CODE",
        help="skip a finding code (repeatable)",
    )
    a.add_argument(
        "--hosted", action="store_true", help="also flag settings the hosted service can't express"
    )
    a.add_argument("--advisories", action="store_true", help="look up package advisories")
    a.add_argument("--depsolve", choices=["composer"], help="resolve exact package versions")
    a.add_argument("--packages", metavar="FILE", help="installed versions (rpm -qa output)")
    a.add_argument("--socket", default=None, help="osbuild-composer socket")
    a.add_argument("--distro", help="distro for advisory lookup (default: the blueprint's)")
    a.set_defaults(func=cmd_audit)

    st = sub.add_parser("status", help="show a compose's status")
    st.add_argument("id")
    st.add_argument("--backend", choices=["composer", "hosted"], default="composer")
    st.add_argument("--socket", default=None)
    st.set_defaults(func=cmd_status)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    extra: list[str] = []
    if "--" in argv:  # `bpkit build ... -- --with-manifest` passes args to image-builder
        cut = argv.index("--")
        argv, extra = argv[:cut], argv[cut + 1 :]
    args = build_parser().parse_args(argv)
    args.extra = extra
    try:
        return int(args.func(args))
    except CLIError as e:
        print(f"bpkit: error: {e}", file=sys.stderr)
        return 2
    except RuntimeError as e:  # ComposerError, HostedError, BuildError
        print(f"bpkit: error: {e}", file=sys.stderr)
        return 1
    except (TypeError, ValueError) as e:
        # Blueprint declaration errors surface here on import.
        print(f"bpkit: error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
