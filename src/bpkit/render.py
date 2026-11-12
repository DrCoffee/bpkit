"""Rendering: blueprint dict, TOML text, and ``explain()``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import tomli_w

from .merge import FIELDS, FieldSpec, KeyedList, StrSet, atoms, deep_merge
from .models import KeyedModel, StructModel, clean

if TYPE_CHECKING:
    from .blueprint import Blueprint

__all__ = ["explain", "render_value", "to_dict", "to_toml"]


def render_value(spec: FieldSpec, value: Any) -> Any:
    """One resolved field value in blueprint shape (``None`` if empty)."""
    if value is None:
        return None
    if spec.kind == "keyed":
        entries = [e.to_blueprint() for e in value]
        return entries or None
    if spec.kind == "set":
        items = list(value)
        if not items:
            return None
        return " ".join(items) if spec.join else items
    if spec.kind == "struct":
        return value.to_blueprint() or None
    if spec.kind == "extra":
        # Passthrough: an empty list or table may be meaningful, so keep it.
        return clean(value, keep_empty=True) or None
    if isinstance(value, StructModel | KeyedModel):  # pragma: no cover - defensive
        return value.to_blueprint()
    return value


def _put(d: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    for key in path[:-1]:
        d = d.setdefault(key, {})
    if isinstance(value, dict) and isinstance(d.get(path[-1]), dict):
        d[path[-1]] = deep_merge(d[path[-1]], value)
    else:
        d[path[-1]] = value


def to_dict(bp: Blueprint) -> dict[str, Any]:
    """A plain dict in blueprint shape, deterministic in key and list order."""
    out: dict[str, Any] = {"name": bp.name}
    if bp.description:
        out["description"] = bp.description
    if bp.version:
        out["version"] = bp.version
    for name, spec in FIELDS.items():
        rendered = render_value(spec, getattr(bp, name))
        if rendered is not None:
            _put(out, spec.path, rendered)
    _check_no_markers(out)
    return out


def _check_no_markers(value: Any, path: str = "") -> None:
    from .markers import Remove, Replace, UnsetType

    if isinstance(value, (Remove, Replace, UnsetType, KeyedList, StrSet)):
        raise AssertionError(f"internal error: {value!r} leaked into output at {path or '/'}")
    if isinstance(value, dict):
        for k, v in value.items():
            _check_no_markers(v, f"{path}.{k}" if path else k)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _check_no_markers(v, f"{path}[{i}]")


def to_toml(data: dict[str, Any]) -> str:
    """Serialise a blueprint dict as TOML (multi-line strings for file data)."""
    return tomli_w.dumps(data, multiline_strings=True)


def explain(target: type[Blueprint] | Blueprint, field: str | None = None) -> str:
    """Which class contributed each value of ``field`` (or of every set field)."""
    from .blueprint import display_name

    if isinstance(target, type):
        state = target._resolved
        provenance = target._provenance
        title = display_name(target)
    else:
        state = target.__dict__
        provenance = target.__dict__["_provenance"]
        title = f"{display_name(type(target))} instance"

    if field is not None and field not in FIELDS:
        from .merge import suggest_field

        raise KeyError(f"unknown field {field!r}{suggest_field(field)}")

    names = [field] if field else [n for n in FIELDS if atoms(FIELDS[n], state[n])]
    lines = [f"# {title}"] if field is None else []
    for name in names:
        rows = [
            (display, provenance.get(name, {}).get(atom_id, "?"))
            for atom_id, (_, display) in atoms(FIELDS[name], state[name]).items()
        ]
        lines.append(f"{name}:")
        if not rows:
            lines.append("  (empty)")
        width = max((len(r[0]) for r in rows), default=0)
        lines.extend(f"  {disp.ljust(width)}  <- {origin}" for disp, origin in rows)
    return "\n".join(lines)
