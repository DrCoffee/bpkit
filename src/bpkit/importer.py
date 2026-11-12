"""Import existing TOML blueprints as Python classes (``bpkit import``).

One blueprint becomes one class. Several blueprints can share a generated
base class holding everything they have in common: entries and settings
identical in every blueprint move to the base, and each subclass keeps only
what differs. Inline file contents can be written out to overlay directories
instead of staying as ``File(...)`` entries.

The generated module renders back to the same blueprint, except that:

* list order may change when a base class is factored out (the base's
  entries come first), and
* files moved into an overlay get an explicit ``mode = "0644"`` when they had
  none. That is image-builder's default anyway.

>>> src = import_blueprints([tomllib.loads(text)])  # doctest: +SKIP
"""

from __future__ import annotations

import copy
import keyword
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .blueprint import kebab_case
from .merge import FIELDS, FieldSpec
from .models import KeyedModel, StructModel

__all__ = ["BlueprintImportError", "ParsedBlueprint", "import_blueprints", "parse_blueprint"]


class BlueprintImportError(ValueError):
    """A blueprint can't be imported."""


# --------------------------------------------------------------------------
# TOML dict → bpkit field values (plain data)
# --------------------------------------------------------------------------

_TOP_IDENTITY = ("name", "description", "version")


@dataclass
class ParsedBlueprint:
    """A blueprint broken into bpkit fields (values are plain dicts/lists)."""

    name: str
    description: str | None = None
    version: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)


def _path_index() -> dict[tuple[str, ...], FieldSpec]:
    return {spec.path: spec for spec in FIELDS.values() if spec.kind != "extra"}


def parse_blueprint(data: Mapping[str, Any]) -> ParsedBlueprint:
    """Split a blueprint dict (as from ``tomllib``) into bpkit field values.

    Anything bpkit doesn't model under ``[customizations]`` lands in
    ``extra_customizations``. Unknown top-level keys are an error.
    """
    data = copy.deepcopy(dict(data))
    if not isinstance(data.get("name"), str) or not data["name"]:
        raise BlueprintImportError("blueprint has no name")
    parsed = ParsedBlueprint(
        name=data.pop("name"),
        description=data.pop("description", None) or None,
        version=data.pop("version", None) or None,
    )
    index = _path_index()
    custom = data.pop("customizations", None) or {}
    if not isinstance(custom, dict):
        raise BlueprintImportError("customizations must be a table")

    for key, value in data.items():
        spec = index.get((key,))
        if spec is None:
            raise BlueprintImportError(f"unknown top-level blueprint key {key!r}")
        if value not in ([], {}, None):
            parsed.fields[spec.name] = value

    extra: dict[str, Any] = {}
    for key, value in custom.items():
        if key == "kernel" and isinstance(value, dict):
            kernel = dict(value)
            append = kernel.pop("append", None)
            if append:
                parsed.fields["kernel_append"] = str(append).split()
            if kernel:
                parsed.fields["kernel"] = kernel
        elif key == "timezone" and isinstance(value, dict):
            tz = dict(value)
            if tz.get("timezone"):
                parsed.fields["timezone"] = tz.pop("timezone")
            else:
                tz.pop("timezone", None)
            if tz.get("ntpservers"):
                parsed.fields["ntpservers"] = tz.pop("ntpservers")
            else:
                tz.pop("ntpservers", None)
            if tz:
                extra["timezone"] = tz
        elif (spec := index.get(("customizations", key))) is not None:
            if value not in ([], {}, None):
                if spec.name == "filesystem":
                    value = [_fs_entry(v) for v in value]
                parsed.fields[spec.name] = value
        else:
            extra[key] = value
    if extra:
        parsed.fields["extra_customizations"] = extra
    _validate(parsed)
    return parsed


def _fs_entry(value: Mapping[str, Any]) -> dict[str, Any]:
    v = dict(value)
    if "size" in v and "minsize" not in v:
        v["minsize"] = v.pop("size")
    return v


def _validate(parsed: ParsedBlueprint) -> None:
    """Fail early (with the blueprint's name) if bpkit would reject a value."""
    from .merge import normalize_value

    for name, value in parsed.fields.items():
        try:
            normalize_value(FIELDS[name], value)
        except Exception as e:
            raise BlueprintImportError(f"{parsed.name}: {name}: {e}") from e


# --------------------------------------------------------------------------
# Factoring a common base out of several blueprints
# --------------------------------------------------------------------------


def _common(spec: FieldSpec, values: list[Any]) -> Any:
    """What every one of ``values`` has in common for this field, or ``None``."""
    if any(v is None for v in values):
        return None
    first = values[0]
    if spec.kind in ("scalar",):
        return first if all(v == first for v in values) else None
    if spec.kind == "keyed":
        return [e for e in first if all(e in v for v in values[1:])] or None
    if spec.kind == "set":
        return [e for e in first if all(e in v for v in values[1:])] or None
    if spec.kind in ("struct", "extra"):
        return _common_dict(values, spec)
    return None


def _common_dict(values: list[Mapping[str, Any]], spec: FieldSpec | None) -> Any:
    keys = [k for k in values[0] if all(k in v for v in values[1:])]
    out: dict[str, Any] = {}
    for k in keys:
        vs = [v[k] for v in values]
        first = vs[0]
        str_list = isinstance(first, list) and all(isinstance(x, str) for x in first)
        if spec is not None and spec.kind == "struct" and str_list:
            common = [e for e in first if all(e in v for v in vs[1:])]
            if common:
                out[k] = common
            continue
        if isinstance(first, dict) and all(isinstance(v, dict) for v in vs):
            sub = _common_dict(vs, spec)
            if sub:
                out[k] = sub
        elif all(v == first for v in vs):
            out[k] = first
    return out or None


def _subtract(spec: FieldSpec, value: Any, base: Any) -> Any:
    """``value`` minus what the base already provides (``None`` if nothing left)."""
    if base is None:
        return value
    if spec.kind == "scalar":
        return None if value == base else value
    if spec.kind in ("keyed", "set"):
        rest = [e for e in value if e not in base]
        return rest or None
    if spec.kind in ("struct", "extra"):
        return _subtract_dict(value, base, struct=spec.kind == "struct")
    return value


def _subtract_dict(value: Mapping[str, Any], base: Mapping[str, Any], *, struct: bool) -> Any:
    out: dict[str, Any] = {}
    for k, v in value.items():
        if k not in base:
            out[k] = v
            continue
        b = base[k]
        if struct and isinstance(v, list) and all(isinstance(x, str) for x in v):
            rest = [e for e in v if e not in b]
            if rest:
                out[k] = rest
        elif isinstance(v, dict) and isinstance(b, dict):
            sub = _subtract_dict(v, b, struct=struct)
            if sub:
                out[k] = sub
        elif v != b:
            out[k] = v
    return out or None


def factor_base(parsed: Sequence[ParsedBlueprint]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Split fields into (common base fields, per-blueprint remaining fields)."""
    base: dict[str, Any] = {}
    for name, spec in FIELDS.items():
        common = _common(spec, [p.fields.get(name) for p in parsed])
        if common:
            base[name] = common
    rest = []
    for p in parsed:
        mine: dict[str, Any] = {}
        for name, value in p.fields.items():
            left = _subtract(FIELDS[name], value, base.get(name))
            if left is not None:
                mine[name] = left
        rest.append(mine)
    return base, rest


# --------------------------------------------------------------------------
# Overlay extraction
# --------------------------------------------------------------------------


def _overlayable(entry: Mapping[str, Any]) -> bool:
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("data"), str)
        and "uri" not in entry
        and entry.get("user") is None
        and entry.get("group") is None
        # An overlay only carries the exec bit; other modes must stay explicit.
        and entry.get("mode") in (None, "0644", "0755")
    )


def extract_overlay(fields: dict[str, Any], directory: Path) -> bool:
    """Move inline file contents in ``fields`` into ``directory``.

    Files with an owner, a URI, no data or a mode other than ``0644`` /
    ``0755`` stay as ``File`` entries. Returns
    whether anything was written.
    """
    files = fields.get("files") or []
    moved = [f for f in files if _overlayable(f)]
    if not moved:
        return False
    for f in moved:
        target = directory / f["path"].lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f["data"], encoding="utf-8")
        target.chmod(int(f.get("mode") or "0644", 8))
    rest = [f for f in files if not _overlayable(f)]
    if rest:
        fields["files"] = rest
    else:
        fields.pop("files", None)
    return True


# --------------------------------------------------------------------------
# Python source generation
# --------------------------------------------------------------------------


def class_name_for(name: str) -> str:
    """``web-server`` → ``WebServer``; always a valid identifier."""
    parts = re.split(r"[^0-9A-Za-z]+", name)
    out = "".join(p[:1].upper() + p[1:] for p in parts if p)
    if not out or out[0].isdigit():
        out = "Bp" + out
    return out + "_" if keyword.iskeyword(out) else out


#: How many leading positional args read naturally per model.
_POSITIONAL = {
    "Package": 2,
    "Filesystem": 2,
    "SSHKey": 2,
    "Group": 2,
    "EnabledModule": 2,
    "Kernel": 1,
    "Services": 0,
    "Firewall": 0,
    "FirewallServices": 0,
    "Locale": 0,
    "Installer": 0,
    "AnacondaModules": 0,
}


def _q(value: Any) -> str:
    """``repr`` with double quotes for strings, like hand-written code."""
    r = repr(value)
    if isinstance(value, str) and r.startswith("'") and '"' not in value:
        r = '"' + r[1:-1].replace("\\'", "'") + '"'
    return r


class _Emitter:
    """Renders bpkit field values as Python source."""

    def __init__(self) -> None:
        self.imports: set[str] = set()

    def literal(self, value: Any, indent: int) -> str:
        if (
            isinstance(value, str)
            and "\n" in value[:-1]
            and '"""' not in value
            and all(c.isprintable() or c in "\n\t" for c in value)
        ):
            body = value.replace("\\", "\\\\")
            if body.endswith('"'):
                return repr(value)
            return '"""' + ("\\\n" if not body.startswith("\n") else "") + body + '"""'
        if isinstance(value, list):
            return self.seq([self.literal(v, indent + 4) for v in value], indent)
        if isinstance(value, dict):
            items = [f"{_q(k)}: {self.literal(v, indent + 4)}" for k, v in value.items()]
            return self.seq(items, indent, "{", "}")
        return _q(value)

    def seq(self, items: list[str], indent: int, open_: str = "[", close: str = "]") -> str:
        flat = open_ + ", ".join(items) + close
        if len(flat) + indent <= 88 and "\n" not in flat:
            return flat
        pad = " " * (indent + 4)
        return open_ + "\n" + "".join(f"{pad}{i},\n" for i in items) + " " * indent + close

    def call(self, name: str, args: list[str], kwargs: list[tuple[str, str]], indent: int) -> str:
        self.imports.add(name)
        parts = args + [f"{k}={v}" for k, v in kwargs]
        flat = f"{name}(" + ", ".join(parts) + ")"
        if len(flat) + indent <= 88 and "\n" not in flat:
            return flat
        pad = " " * (indent + 4)
        return f"{name}(\n" + "".join(f"{pad}{p},\n" for p in parts) + " " * indent + ")"

    def model(self, model: type[Any], data: Mapping[str, Any], indent: int) -> str:
        if issubclass(model, KeyedModel):
            short = self.shorthand(model, data)
            if short is not None:
                return _q(short)
        by_alias = {f.alias: n for n, f in model.model_fields.items() if f.alias}
        values = {by_alias.get(k, k): v for k, v in data.items()}
        if model.__name__ == "Filesystem" and "size" in values:
            values["minsize"] = values.pop("size")
        args: list[str] = []
        for pname in model.__positional__[: _POSITIONAL.get(model.__name__, 1)]:
            if pname not in values:
                break
            args.append(self.value(model, pname, values.pop(pname), indent + 4))
        kwargs = [(k, self.value(model, k, v, indent + 4)) for k, v in values.items()]
        return self.call(model.__name__, args, kwargs, indent)

    def value(self, model: type[Any], fname: str, v: Any, indent: int) -> str:
        if issubclass(model, StructModel) and isinstance(v, (dict, list)):
            kind = model.__merge__.get(fname)
            if kind == "struct":
                from .models import struct_model_for

                return self.model(struct_model_for(model, fname), v, indent)
            if kind == "keyed":
                from .models import keyed_model_for

                sub = keyed_model_for(model, fname)
                return self.seq([self.model(sub, e, indent + 4) for e in v], indent)
        return self.literal(v, indent)

    @staticmethod
    def shorthand(model: type[KeyedModel], data: Mapping[str, Any]) -> str | None:
        name = model.__name__
        if name == "Package" and set(data) <= {"name", "version"}:
            return data["name"] if data.get("version", "*") == "*" else None
        if name == "PackageGroup" and set(data) == {"name"}:
            return data["name"]
        if name == "EnabledModule" and set(data) <= {"name", "stream"}:
            return f"{data['name']}:{data['stream']}" if data.get("stream") else data["name"]
        if name == "Container" and set(data) == {"source"}:
            return data["source"]
        return None

    def field(self, spec: FieldSpec, value: Any) -> str:
        if spec.kind == "keyed":
            return self.seq([self.model(spec.type, e, 8) for e in value], 4)
        if spec.kind == "struct":
            return self.model(spec.type, value, 4)
        return self.literal(value, 4)


def _render_class(
    em: _Emitter,
    cls_name: str,
    bases: str,
    *,
    name: str | None,
    description: str | None,
    version: str | None,
    fields: Mapping[str, Any],
    overlay: str | None,
    abstract: bool = False,
) -> str:
    head = f"class {cls_name}({bases}{', abstract=True' if abstract else ''}):"
    body: list[str] = []
    plain = description and not any(s in description for s in ("\n", "\\", '"""'))
    if plain and description == description.strip() and not description.endswith('"'):
        body.append(f'    """{description}"""')
        body.append("")
    elif description:
        body.append(f"    description = {_q(description)}")
    if name is not None and kebab_case(cls_name) != name:
        body.append(f"    name = {_q(name)}")
    if version and version != "0.0.1":
        body.append(f"    version = {_q(version)}")
    for fname, spec in FIELDS.items():
        if fname in fields:
            body.append(f"    {fname} = {em.field(spec, fields[fname])}")
    if overlay is not None:
        em.imports.add("Overlay")
        body.append(f'    overlay = Overlay({_q(overlay)}, directories="none")')
    while body and body[-1] == "":
        body.pop()
    if not body:
        body = ["    pass"]
    return head + "\n" + "\n".join(body) + "\n"


def import_blueprints(
    blueprints: Iterable[Mapping[str, Any]],
    *,
    base: str | None = None,
    overlay_dir: str | Path | None = None,
    module_dir: str | Path | None = None,
    source: str = "bpkit import",
) -> str:
    """Generate a Python module defining one class per blueprint.

    :param base: name of a class to factor common settings into (needs at
        least two blueprints). It is generated with ``abstract=True``.
    :param overlay_dir: write inline file contents into
        ``overlay_dir/<kebab-cased class name>/`` (and ``overlay_dir/<base>/``) and
        reference them with ``Overlay(...)``.
    :param module_dir: where the generated module will live, so overlay paths
        can be written relative to it. Defaults to the current directory.
    """
    parsed = [parse_blueprint(b) for b in blueprints]
    if not parsed:
        raise BlueprintImportError("nothing to import")
    names = [class_name_for(p.name) for p in parsed]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise BlueprintImportError(f"blueprints map to the same class name: {sorted(dupes)}")

    if base is not None and len(parsed) < 2:
        raise BlueprintImportError("--base needs at least two blueprints")
    if base is not None and not base.isidentifier():
        raise BlueprintImportError(f"invalid base class name {base!r}")
    if base is not None and base in names:
        raise BlueprintImportError(f"base class name {base!r} clashes with an imported blueprint")

    if base is not None:
        base_fields, per = factor_base(parsed)
    else:
        base_fields, per = {}, [dict(p.fields) for p in parsed]

    root = Path(module_dir or ".").resolve()
    out_root = Path(overlay_dir).resolve() if overlay_dir is not None else None

    def overlay_for(fields: dict[str, Any], dirname: str) -> str | None:
        if out_root is None:
            return None
        target = out_root / dirname
        if not extract_overlay(fields, target):
            return None
        try:
            return target.relative_to(root).as_posix()
        except ValueError:
            return str(target)

    em = _Emitter()
    em.imports.add("Blueprint")
    classes: list[str] = []
    if base is not None:
        classes.append(
            _render_class(
                em,
                base,
                "Blueprint",
                name=None,
                description="Settings shared by every imported blueprint.",
                version=None,
                fields=base_fields,
                overlay=overlay_for(base_fields, kebab_case(base)),
                abstract=True,
            )
        )
    for p, cls_name, fields in zip(parsed, names, per, strict=True):
        classes.append(
            _render_class(
                em,
                cls_name,
                base or "Blueprint",
                name=p.name,
                description=p.description,
                version=p.version or "0.0.1",
                fields=fields,
                overlay=overlay_for(fields, kebab_case(cls_name)),
            )
        )
    imports = ", ".join(sorted(em.imports, key=lambda s: (s != "Blueprint", s)))
    header = f'"""Generated by {source}. Edit freely."""\n\nfrom bpkit import {imports}\n'
    return header + "\n\n" + "\n\n".join(classes)


def load_toml_files(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    import tomllib

    out = []
    for path in paths:
        with open(path, "rb") as f:
            try:
                out.append(tomllib.load(f))
            except tomllib.TOMLDecodeError as e:
                raise BlueprintImportError(f"{path}: {e}") from None
    return out
