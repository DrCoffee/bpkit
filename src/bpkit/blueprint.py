"""The :class:`Blueprint` base class and class-definition-time resolution."""

from __future__ import annotations

import copy
import functools
import inspect
import os
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, ClassVar, TypeVar

from pydantic import ValidationError

from .markers import Remove, Replace, Unset
from .merge import (
    FIELDS,
    MergeError,
    apply_layer,
    apply_value,
    empty_value,
    normalize_value,
    suggest_field,
    track,
)
from .overlay import Overlay

__all__ = ["Blueprint", "BlueprintError", "kebab_case"]

IDENTITY = ("name", "description", "version")
DEFAULT_VERSION = "0.0.1"
OVERLAY = "overlay"


class BlueprintError(TypeError):
    """A blueprint class or instance is declared incorrectly."""


def kebab_case(name: str) -> str:
    """``StagingWeb`` → ``staging-web``, ``HTTPServer2`` → ``http-server2``."""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "-", name)
    return s.replace("_", "-").lower()


def _first_line(doc: str | None) -> str | None:
    if not doc:
        return None
    for line in inspect.cleandoc(doc).splitlines():
        if line.strip():
            return line.strip()
    return None


def display_name(cls: type) -> str:
    """``__qualname__`` without ``<locals>`` noise for classes made in functions."""
    return cls.__qualname__.rsplit("<locals>.", 1)[-1]


def _module_dir(module_name: str) -> Path | None:
    module = sys.modules.get(module_name)
    file = getattr(module, "__file__", None)
    return Path(file).resolve().parent if file else None


def _is_declaration(name: str, value: Any) -> bool:
    """Whether a class attribute looks like a (possibly misspelt) field."""
    if name.startswith("_") or not name[:1].islower():
        return False  # private names and CONSTANTS are the user's business
    if isinstance(value, (staticmethod, classmethod, property, functools.cached_property, type)):
        return False
    return not (
        callable(value) or inspect.isdatadescriptor(value) or inspect.ismethoddescriptor(value)
    )


def normalize_layer(raw: Mapping[str, Any], *, owner: str, base_dir: Path | None) -> dict[str, Any]:
    """Normalise a set of declarations (a class ``__dict__`` or kwargs)."""
    layer: dict[str, Any] = {}
    for name, value in raw.items():
        if name in IDENTITY or name == OVERLAY:
            continue
        if name not in FIELDS:
            if _is_declaration(name, value):
                raise BlueprintError(
                    f"{owner}: unknown blueprint field {name!r}{suggest_field(name)}. "
                    "Use an _underscore or UPPER_CASE name for your own attributes."
                )
            continue
        try:
            layer[name] = normalize_value(FIELDS[name], value)
        except (MergeError, ValidationError, TypeError, ValueError) as e:
            if isinstance(e, ValidationError):
                e.add_note(f"in {owner}.{name}")
                raise
            raise BlueprintError(f"{owner}.{name}: {e}") from None

    if OVERLAY in raw and raw[OVERLAY] is not None:
        _expand_overlays(layer, raw[OVERLAY], owner=owner, base_dir=base_dir)
    return layer


def _expand_overlays(
    layer: dict[str, Any], value: Any, *, owner: str, base_dir: Path | None
) -> None:
    """Fold overlay entries into the layer's ``files`` / ``directories``.

    Overlay entries come first; explicit ``files`` / ``directories`` entries in
    the same class win over overlay entries with the same path.
    """
    items = value if isinstance(value, (list, tuple)) else [value]
    dirs: dict[str, Any] = {}
    files: dict[str, Any] = {}
    for item in items:
        if isinstance(item, (str, os.PathLike)):
            item = Overlay(item)
        if not isinstance(item, Overlay):
            raise BlueprintError(f"{owner}.overlay: expected Overlay or a path, got {item!r}")
        d, f = item.resolved(base_dir).entries()
        dirs.update((e.path, e) for e in d)
        files.update((e.path, e) for e in f)

    for fname, entries in (("directories", dirs), ("files", files)):
        if not entries:
            continue
        explicit = layer.get(fname)
        if isinstance(explicit, Replace):
            # Replace discards what's inherited, not this class's own overlay.
            merged = {**entries, **{e.path: e for e in explicit.value}}
            layer[fname] = Replace(list(merged.values()))
        elif explicit is Unset:
            raise BlueprintError(f"{owner}: can't combine overlay with {fname} = Unset")
        else:
            explicit = explicit or []
            keys = {getattr(e, "path", None) for e in explicit}
            layer[fname] = [e for p, e in entries.items() if p not in keys] + list(explicit)


B = TypeVar("B", bound="Blueprint")


class HybridMethod:
    """A method that works on both the class and its instances."""

    def __init__(self, func: Callable[..., Any]) -> None:
        self.func = func
        functools.update_wrapper(self, func)  # type: ignore[arg-type]

    def __get__(self, obj: Any, objtype: type | None = None) -> Callable[..., Any]:
        return functools.partial(self.func, obj if obj is not None else objtype)


class Blueprint:
    """Base class for Python-defined image-builder blueprints.

    Declare fields as class attributes. Lists merge with what's inherited,
    scalars override, and ``Remove`` / ``Replace`` / ``Unset`` adjust inherited
    values. See the README for the full merge rules.

    Pass ``abstract=True`` in the class statement for bases and mixins that
    bulk rendering should skip::

        class Hardened(Blueprint, abstract=True): ...
    """

    # Resolved per class in __init_subclass__.
    _layer: ClassVar[dict[str, Any]] = {}
    _resolved: ClassVar[dict[str, Any]] = {}
    _provenance: ClassVar[dict[str, dict[str, str]]] = {}
    _identity: ClassVar[dict[str, str | None]] = {}
    _abstract: ClassVar[bool] = True

    # Populated on instances (typed loosely: class bodies assign shorthands).
    name: str
    description: str | None
    version: str

    def __init_subclass__(cls, *, abstract: bool = False, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls._abstract = abstract
        owner = display_name(cls)
        own = dict(cls.__dict__)

        identity: dict[str, str | None] = {
            "name": own.get("name", kebab_case(cls.__name__)),
            "description": own.get("description", _first_line(own.get("__doc__"))),
            "version": own.get("version", DEFAULT_VERSION),
        }
        for key, val in identity.items():
            if val is not None and not isinstance(val, str):
                raise BlueprintError(f"{owner}.{key} must be a string, got {val!r}")
        cls._identity = identity

        cls._layer = normalize_layer(own, owner=owner, base_dir=_module_dir(cls.__module__))

        state = {name: empty_value(spec) for name, spec in FIELDS.items()}
        provenance: dict[str, dict[str, str]] = {}
        for klass in reversed(cls.__mro__):
            if not issubclass(klass, Blueprint) or klass is Blueprint:
                continue
            # Abstract mixins may Remove things they don't inherit themselves;
            # the check happens when a concrete class pulls them in.
            try:
                apply_layer(
                    state, provenance, klass._layer, display_name(klass), strict=not abstract
                )
            except MergeError as e:
                where = "" if klass is cls else f" (while resolving {owner})"
                raise BlueprintError(f"{e}{where}") from None
        cls._resolved = state
        cls._provenance = provenance

    def __init__(self, **kwargs: Any) -> None:
        cls = type(self)
        owner = f"{display_name(cls)}(...)"
        state = copy.deepcopy(cls._resolved)
        provenance = copy.deepcopy(cls._provenance)
        identity = dict(cls._identity)

        for key in IDENTITY:
            if key in kwargs:
                val = kwargs.pop(key)
                if val is not None and not isinstance(val, str):
                    raise BlueprintError(f"{owner}: {key} must be a string, got {val!r}")
                identity[key] = val
        unknown = [k for k in kwargs if k not in FIELDS and k != OVERLAY]
        if unknown:
            raise BlueprintError(
                f"{owner}: unknown blueprint field {unknown[0]!r}{suggest_field(unknown[0])}"
            )
        layer = normalize_layer(kwargs, owner=owner, base_dir=Path.cwd())
        try:
            apply_layer(state, provenance, layer, "<kwargs>")
        except MergeError as e:
            raise BlueprintError(str(e)) from None

        d = self.__dict__
        d.update(identity)
        d.update(state)
        d["_provenance"] = provenance

        before = {name: copy.deepcopy(d[name]) for name in FIELDS}
        self.customize()
        for name in FIELDS:
            track(provenance, name, before[name], d[name], "customize()")

    def customize(self) -> None:
        """Hook run on each instance after resolution.

        Override it for logic (env vars, conditionals). Call
        ``super().customize()`` so mixins' hooks run too.
        """

    def __setattr__(self, name: str, value: Any) -> None:
        if name in FIELDS:
            spec = FIELDS[name]
            try:
                norm = normalize_value(spec, value)
                if isinstance(norm, Replace):
                    norm = norm.value
                if (
                    spec.kind in ("keyed", "set")
                    and norm is not Unset
                    and any(isinstance(i, Remove) for i in norm)
                ):
                    raise MergeError(
                        f"assignment replaces the whole value; use self.{name}.remove() instead"
                    )
                empty = empty_value(spec)
                value = empty if norm is Unset else apply_value(spec, empty, Replace(norm))
            except MergeError as e:
                raise BlueprintError(f"{display_name(type(self))}.{name}: {e}") from None
        elif name in IDENTITY:
            if value is not None and not isinstance(value, str):
                raise BlueprintError(f"{name} must be a string, got {value!r}")
        object.__setattr__(self, name, value)

    # -- output -------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """The blueprint as a plain dict (TOML names, empty fields omitted)."""
        from .render import to_dict

        return to_dict(self)

    def to_toml(self) -> str:
        """The blueprint as TOML text."""
        from .render import to_toml

        return to_toml(self.to_dict())

    def write(self, path: str | os.PathLike[str]) -> Path:
        """Write the TOML to ``path`` and return it."""
        out = Path(path)
        out.write_text(self.to_toml(), encoding="utf-8")
        return out

    @HybridMethod
    def explain(target: Any, field: str | None = None) -> str:  # noqa: N805
        """Show which class contributed each value of ``field`` (or all fields).

        Works on a class (resolution only) or an instance (also kwargs and
        ``customize()``).
        """
        from .render import explain

        return explain(target, field)

    @classmethod
    def is_abstract(cls) -> bool:
        return cls.__dict__.get("_abstract", True)

    @classmethod
    def blueprint_bases(cls) -> list[type[Blueprint]]:
        """Direct bases that are blueprints."""
        return [b for b in cls.__bases__ if issubclass(b, Blueprint) and b is not Blueprint]

    def __repr__(self) -> str:
        return f"<{display_name(type(self))} {self.name!r} v{self.version}>"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Blueprint):
            return NotImplemented
        return self.to_dict() == other.to_dict()

    __hash__ = None  # type: ignore[assignment]


def iter_blueprints(objects: Iterable[Any]) -> list[type[Blueprint]]:
    """Blueprint subclasses among ``objects``, de-duplicated, in order."""
    seen: list[type[Blueprint]] = []
    for obj in objects:
        is_bp = isinstance(obj, type) and issubclass(obj, Blueprint) and obj is not Blueprint
        if is_bp and obj not in seen:
            seen.append(obj)
    return seen
