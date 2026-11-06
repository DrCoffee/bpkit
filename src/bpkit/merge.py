"""Merge engine: field registry, collection types, normalisation and merge rules.

A *layer* is one set of declarations: a class's own ``__dict__``, constructor
kwargs, or an assignment in ``customize()``. Layers are normalised (shorthands
→ models, markers kept) and then applied in order onto the accumulated state.
"""

from __future__ import annotations

import copy
import difflib
import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar, overload

from pydantic import StrictBool, TypeAdapter

from .markers import Remove, Replace, Unset, UnsetType
from .models import (
    Container,
    Directory,
    EnabledModule,
    File,
    Filesystem,
    Firewall,
    Group,
    Installer,
    Kernel,
    KeyedModel,
    Locale,
    Package,
    PackageGroup,
    PartitioningMode,
    Repository,
    Services,
    SSHKey,
    StructModel,
    User,
    keyed_model_for,
    struct_model_for,
)

__all__ = [
    "FIELDS",
    "FieldSpec",
    "KeyedList",
    "MergeError",
    "StrSet",
    "apply_layer",
    "atoms",
    "default_state",
    "normalize_value",
]


class MergeError(ValueError):
    """A declaration can't be merged (bad Remove, duplicate key, wrong type…)."""


# --------------------------------------------------------------------------
# Collection types used on resolved blueprints
# --------------------------------------------------------------------------

M = TypeVar("M", bound=KeyedModel)


class KeyedList(Generic[M]):
    """An ordered list of entries unique by key (``Package.name``, ``File.path``…).

    Adding an entry whose key already exists replaces it in place, keeping the
    original position. Accepts the same shorthands as class declarations.
    """

    __slots__ = ("_items", "model")

    def __init__(self, model: type[M], items: Iterable[Any] = ()) -> None:
        self.model = model
        self._items: dict[Any, M] = {}
        self.add(*items)

    def _coerce(self, item: Any) -> M:
        return coerce_entry(self.model, item)

    def add(self, *items: Any) -> None:
        """Add entries; an entry with an existing key replaces it."""
        for item in items:
            entry = self._coerce(item)
            self._items[entry.merge_key] = entry

    def remove(self, *keys: Any) -> None:
        """Remove entries by key; raises ``KeyError`` if a key is missing."""
        for key in keys:
            key = self._key_of(key)
            if key not in self._items:
                raise KeyError(f"{key!r} not in {self._describe()}")
            del self._items[key]

    def discard(self, *keys: Any) -> None:
        """Remove entries by key if present."""
        for key in keys:
            self._items.pop(self._key_of(key), None)

    def clear(self) -> None:
        self._items.clear()

    def get(self, key: Any, default: Any = None) -> M | Any:
        return self._items.get(key, default)

    def keys(self) -> list[Any]:
        return list(self._items)

    def _key_of(self, key: Any) -> Any:
        return key.merge_key if isinstance(key, KeyedModel) else key

    def _describe(self) -> str:
        keys = ", ".join(map(repr, self._items)) or "nothing"
        return f"{self.model.__name__} entries ({keys})"

    def __contains__(self, key: object) -> bool:
        return self._key_of(key) in self._items

    def __iter__(self) -> Iterator[M]:
        return iter(list(self._items.values()))

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)

    @overload
    def __getitem__(self, index: int) -> M: ...
    @overload
    def __getitem__(self, index: str) -> M: ...
    def __getitem__(self, index: Any) -> M:
        if isinstance(index, int) and not isinstance(index, bool):
            return list(self._items.values())[index]
        return self._items[index]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, KeyedList):
            return list(self) == list(other)
        if isinstance(other, (list, tuple)):
            try:
                return list(self) == [self._coerce(o) for o in other]
            except (TypeError, ValueError):
                return False
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"KeyedList[{self.model.__name__}]({list(self._items.values())!r})"


class StrSet:
    """An ordered set of strings (``kernel_append``, ``ntpservers``)."""

    __slots__ = ("_items",)

    def __init__(self, items: Iterable[str] = ()) -> None:
        self._items: dict[str, None] = {}
        self.add(*items)

    def add(self, *items: str) -> None:
        for item in items:
            if not isinstance(item, str):
                raise TypeError(f"expected a string, got {item!r}")
            self._items[item] = None

    def remove(self, *items: str) -> None:
        for item in items:
            if item not in self._items:
                raise KeyError(f"{item!r} not in {list(self._items)}")
            del self._items[item]

    def discard(self, *items: str) -> None:
        for item in items:
            self._items.pop(item, None)

    def clear(self) -> None:
        self._items.clear()

    def __contains__(self, item: object) -> bool:
        return item in self._items

    def __iter__(self) -> Iterator[str]:
        return iter(list(self._items))

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, StrSet):
            return list(self) == list(other)
        if isinstance(other, (list, tuple)):
            return list(self) == list(other)
        if isinstance(other, (set, frozenset)):
            return set(self) == other
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"StrSet({list(self._items)!r})"


def coerce_entry(model: type[M], item: Any) -> M:
    """Turn a shorthand (str, dict) or model into an entry of ``model``."""
    if isinstance(item, model):
        return item.model_copy(deep=True)
    if isinstance(item, str):
        return model.from_shorthand(item)  # type: ignore[return-value]
    if isinstance(item, Mapping):
        return model.model_validate(dict(item))
    if isinstance(item, (Remove, Replace, UnsetType)):
        raise MergeError(f"{item!r} is a marker and can't be used here")
    raise MergeError(f"expected {model.__name__}, str or dict, got {type(item).__name__}: {item!r}")


# --------------------------------------------------------------------------
# Field registry
# --------------------------------------------------------------------------

Kind = Literal["scalar", "keyed", "set", "struct", "extra"]


@dataclass(frozen=True)
class FieldSpec:
    """How a blueprint field is typed, merged and rendered."""

    name: str
    kind: Kind
    #: Scalar type, keyed entry model, or struct model.
    type: Any
    #: Where the value goes in the blueprint dict.
    path: tuple[str, ...]
    #: For ``set`` fields rendered as one space-separated string.
    join: bool = False

    @property
    def adapter(self) -> TypeAdapter[Any]:
        return _adapters[self.name]


_C = "customizations"

FIELDS: dict[str, FieldSpec] = {
    f.name: f
    for f in [
        FieldSpec("distro", "scalar", str, ("distro",)),
        FieldSpec("architecture", "scalar", str, ("architecture",)),
        FieldSpec("packages", "keyed", Package, ("packages",)),
        FieldSpec("modules", "keyed", Package, ("modules",)),
        FieldSpec("enabled_modules", "keyed", EnabledModule, ("enabled_modules",)),
        FieldSpec("groups", "keyed", PackageGroup, ("groups",)),
        FieldSpec("containers", "keyed", Container, ("containers",)),
        FieldSpec("hostname", "scalar", str, (_C, "hostname")),
        FieldSpec("kernel", "struct", Kernel, (_C, "kernel")),
        FieldSpec("kernel_append", "set", str, (_C, "kernel", "append"), join=True),
        FieldSpec("sshkeys", "keyed", SSHKey, (_C, "sshkey")),
        FieldSpec("users", "keyed", User, (_C, "user")),
        FieldSpec("user_groups", "keyed", Group, (_C, "group")),
        FieldSpec("timezone", "scalar", str, (_C, "timezone", "timezone")),
        FieldSpec("ntpservers", "set", str, (_C, "timezone", "ntpservers")),
        FieldSpec("locale", "struct", Locale, (_C, "locale")),
        FieldSpec("firewall", "struct", Firewall, (_C, "firewall")),
        FieldSpec("services", "struct", Services, (_C, "services")),
        FieldSpec("filesystem", "keyed", Filesystem, (_C, "filesystem")),
        FieldSpec("installation_device", "scalar", str, (_C, "installation_device")),
        FieldSpec("partitioning_mode", "scalar", PartitioningMode, (_C, "partitioning_mode")),
        FieldSpec("directories", "keyed", Directory, (_C, "directories")),
        FieldSpec("files", "keyed", File, (_C, "files")),
        FieldSpec("repositories", "keyed", Repository, (_C, "repositories")),
        FieldSpec("fips", "scalar", StrictBool, (_C, "fips")),
        FieldSpec("installer", "struct", Installer, (_C, "installer")),
        FieldSpec("extra_customizations", "extra", dict, (_C,)),
    ]
}

_adapters: dict[str, TypeAdapter[Any]] = {
    f.name: TypeAdapter(f.type) for f in FIELDS.values() if f.kind == "scalar"
}


def suggest_field(name: str) -> str:
    """A ``did you mean`` hint for an unknown field name."""
    close = difflib.get_close_matches(name, list(FIELDS), n=1)
    return f" (did you mean {close[0]!r}?)" if close else ""


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


def empty_value(spec: FieldSpec) -> Any:
    if spec.kind == "keyed":
        return KeyedList(spec.type)
    if spec.kind == "set":
        return StrSet()
    if spec.kind == "struct":
        return materialize(spec.type())
    if spec.kind == "extra":
        return {}
    return None


def default_state() -> dict[str, Any]:
    return {name: empty_value(spec) for name, spec in FIELDS.items()}


def materialize(struct: StructModel) -> StructModel:
    """Fill a resolved struct's ``None`` sets/structs with empty values so
    ``customize()`` code can mutate them directly."""
    for fname, kind in struct.__merge__.items():
        value = getattr(struct, fname)
        if kind in ("set", "keyed") and value is None:
            setattr(struct, fname, [])
        elif kind == "struct":
            if value is None:
                value = struct_model_for(type(struct), fname)()
                setattr(struct, fname, value)
            materialize(value)
    return struct


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------


def normalize_value(spec: FieldSpec, raw: Any) -> Any:
    """Normalise one declared value. Markers are kept; shorthands are expanded."""
    if raw is Unset:
        return Unset
    if isinstance(raw, Replace):
        inner = _normalize_plain(spec, raw.value)
        if spec.kind in ("keyed", "set") and any(isinstance(i, Remove) for i in inner):
            raise MergeError("Remove() makes no sense inside Replace()")
        return Replace(inner)
    return _normalize_plain(spec, raw)


def _normalize_plain(spec: FieldSpec, raw: Any) -> Any:
    if spec.kind == "scalar":
        if raw is None:
            raise MergeError("use Unset (not None) to remove an inherited value")
        return spec.adapter.validate_python(raw)

    if spec.kind == "keyed":
        if isinstance(raw, KeyedList):
            raw = list(raw)
        if isinstance(raw, (str, Mapping, KeyedModel)) or not isinstance(raw, Iterable):
            raise MergeError(
                f"expected a list of {spec.type.__name__} entries, got {type(raw).__name__}"
            )
        out: list[Any] = []
        seen: set[Any] = set()
        for item in raw:
            if isinstance(item, Remove):
                out.append(item)
                continue
            entry = coerce_entry(spec.type, item)
            if entry.merge_key in seen:
                raise MergeError(f"duplicate {spec.type.__key__} {entry.merge_key!r}")
            seen.add(entry.merge_key)
            out.append(entry)
        return out

    if spec.kind == "set":
        if isinstance(raw, str):
            # "audit=1 quiet" for kernel_append; a single item otherwise.
            raw = raw.split() if spec.join else [raw]
        if isinstance(raw, StrSet):
            raw = list(raw)
        if not isinstance(raw, Iterable):
            raise MergeError(f"expected a list of strings, got {type(raw).__name__}")
        items: list[Any] = []
        for item in raw:
            if not isinstance(item, (str, Remove)):
                raise MergeError(f"expected a string, got {type(item).__name__}: {item!r}")
            if item not in items:
                items.append(item)
        return items

    if spec.kind == "struct":
        if isinstance(raw, spec.type):
            return raw.model_copy(deep=True)
        if isinstance(raw, Mapping):
            return spec.type.model_validate(dict(raw))
        raise MergeError(f"expected {spec.type.__name__} or dict, got {type(raw).__name__}")

    if spec.kind == "extra":
        if not isinstance(raw, Mapping):
            raise MergeError(f"expected a dict, got {type(raw).__name__}")
        return copy.deepcopy(dict(raw))

    raise AssertionError(spec.kind)  # pragma: no cover


# --------------------------------------------------------------------------
# Applying layers
# --------------------------------------------------------------------------


def apply_value(spec: FieldSpec, acc: Any, layer: Any, *, strict: bool = True) -> Any:
    """Apply one normalised layer value onto the accumulated value."""
    if spec.kind == "scalar":
        if layer is Unset:
            return None
        return layer.value if isinstance(layer, Replace) else layer

    if spec.kind == "keyed":
        return _apply_keyed(spec.type, acc, layer, strict=strict)

    if spec.kind == "set":
        return StrSet(_apply_set(list(acc), layer, strict=strict)[0])

    if spec.kind == "struct":
        return _apply_struct(spec.type, acc, layer, strict=strict)

    if spec.kind == "extra":
        if layer is Unset:
            return {}
        if isinstance(layer, Replace):
            return copy.deepcopy(layer.value)
        return deep_merge(acc, layer)

    raise AssertionError(spec.kind)  # pragma: no cover


def _apply_keyed(model: type[M], acc: KeyedList[M], layer: Any, *, strict: bool) -> KeyedList[M]:
    if layer is Unset:
        return KeyedList(model)
    if isinstance(layer, Replace):
        return KeyedList(model, layer.value)
    out = copy.deepcopy(acc)
    for item in layer:
        if isinstance(item, Remove):
            for key in item.keys:
                if key in out:
                    out.remove(key)
                elif strict:
                    known = ", ".join(map(repr, out.keys())) or "nothing"
                    raise MergeError(
                        f"cannot Remove({key!r}): no inherited {model.__name__} with "
                        f"{model.__key__} {key!r} (inherited: {known})"
                    )
        else:
            out.add(item)
    return out


def _apply_set(acc: list[str], layer: Any, *, strict: bool) -> tuple[list[str], list[str]]:
    """Returns (new list, items the layer asserted)."""
    if layer is Unset or layer is None:
        return ([] if layer is Unset else list(acc)), []
    if isinstance(layer, Replace):
        return list(layer.value), list(layer.value)
    out = list(acc)
    added: list[str] = []
    for item in layer:
        if isinstance(item, Remove):
            for key in item.keys:
                if key in out:
                    out.remove(key)
                elif strict:
                    raise MergeError(f"cannot Remove({key!r}): not inherited (inherited: {out})")
        else:
            if item not in out:
                out.append(item)
            added.append(item)
    return out, added


def _apply_struct(
    model: type[StructModel], acc: StructModel, layer: Any, *, strict: bool
) -> StructModel:
    if layer is Unset:
        return materialize(model())
    if isinstance(layer, Replace):
        return _apply_struct(model, materialize(model()), layer.value, strict=strict)
    out = acc.model_copy(deep=True)
    asserted: dict[str, list[str]] = {}
    for fname, kind in model.__merge__.items():
        if fname not in layer.model_fields_set:
            continue
        value = getattr(layer, fname)
        try:
            if kind == "scalar":
                if value is not None:
                    setattr(out, fname, None if value is Unset else value)
            elif kind == "set":
                new, added = _apply_set(getattr(out, fname) or [], value, strict=strict)
                setattr(out, fname, new)
                asserted[fname] = added
            elif kind == "keyed":
                if value is None:
                    continue
                entry_model = keyed_model_for(model, fname)
                current = KeyedList(entry_model, getattr(out, fname) or [])
                if not isinstance(value, (Replace, UnsetType)):
                    value = [
                        v if isinstance(v, Remove) else coerce_entry(entry_model, v) for v in value
                    ]
                elif isinstance(value, Replace):
                    value = Replace([coerce_entry(entry_model, v) for v in value.value])
                setattr(out, fname, list(_apply_keyed(entry_model, current, value, strict=strict)))
            elif kind == "struct":
                if value is None:
                    continue
                sub = struct_model_for(model, fname)
                if isinstance(value, Replace) and not isinstance(value.value, sub):
                    value = Replace(sub.model_validate(value.value))
                setattr(out, fname, _apply_struct(sub, getattr(out, fname), value, strict=strict))
        except MergeError as e:
            raise MergeError(f"{fname}: {e}") from None
    # Cross-field rule: what this layer asserts wins, and moves the item.
    for group in model.__exclusive__:
        for fname in group:
            for item in asserted.get(fname, ()):
                for other in group:
                    if other != fname:
                        setattr(out, other, [i for i in getattr(out, other) if i != item])
    return materialize(out)


def deep_merge(base: dict[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-merge ``over`` onto a copy of ``base``: dicts recurse, anything else replaces."""
    out = copy.deepcopy(base)
    for k, v in over.items():
        if v is Unset:
            out.pop(k, None)
        elif isinstance(v, Mapping) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def apply_layer(
    state: dict[str, Any],
    provenance: dict[str, dict[str, str]],
    layer: Mapping[str, Any],
    label: str,
    *,
    strict: bool = True,
) -> None:
    """Apply a normalised layer onto ``state`` in place, tracking provenance."""
    for fname in FIELDS:
        if fname not in layer:
            continue
        spec = FIELDS[fname]
        try:
            new = apply_value(spec, state[fname], layer[fname], strict=strict)
        except MergeError as e:
            raise MergeError(f"{label}.{fname}: {e}") from None
        track(provenance, fname, state[fname], new, label)
        state[fname] = new


# --------------------------------------------------------------------------
# Provenance ("which class contributed this value?")
# --------------------------------------------------------------------------


def atoms(spec: FieldSpec, value: Any) -> dict[str, tuple[str, str]]:
    """Flatten a resolved value into ``{atom id: (fingerprint, display)}``."""
    return _atoms(spec.kind, spec.type, value, "")


def _fp(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=repr)


def _atoms(kind: str, tp: Any, value: Any, prefix: str) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    if kind == "scalar":
        if value is not None:
            if isinstance(value, StructModel | KeyedModel):
                shown: Any = value.to_blueprint()
            else:
                shown = value
            out[prefix] = (_fp(shown), f"{prefix} = {shown!r}" if prefix else repr(shown))
    elif kind == "set":
        for item in value or ():
            out[f"{prefix}[{item}]"] = (item, f"{prefix}: {item}" if prefix else item)
    elif kind == "keyed":
        for entry in value or ():
            d = entry.to_blueprint()
            shown = str(entry.merge_key)
            out[f"{prefix}[{entry.merge_key!r}]"] = (
                _fp(d),
                f"{prefix}: {shown}" if prefix else shown,
            )
    elif kind == "struct":
        if value is None:
            return out
        for fname, fkind in type(value).__merge__.items():
            sub_tp = struct_model_for(type(value), fname) if fkind == "struct" else None
            p = f"{prefix}.{fname}" if prefix else fname
            out.update(_atoms(fkind, sub_tp, getattr(value, fname), p))
    elif kind == "extra":
        for k, v in (value or {}).items():
            p = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict) and v:
                out.update(_atoms("extra", None, v, p))
            else:
                out[p] = (_fp(v), f"{p} = {v!r}")
    return out


def track(
    provenance: dict[str, dict[str, str]], fname: str, before: Any, after: Any, label: str
) -> None:
    spec = FIELDS[fname]
    old = atoms(spec, before)
    new = atoms(spec, after)
    prov = provenance.setdefault(fname, {})
    for atom_id, (fp, _) in new.items():
        if atom_id not in old or old[atom_id][0] != fp:
            prov[atom_id] = label
    for atom_id in list(prov):
        if atom_id not in new:
            del prov[atom_id]
