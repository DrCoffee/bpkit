"""Typed blueprint structs (Pydantic v2).

Field names follow the stable blueprint TOML API as implemented in
``github.com/osbuild/blueprint/pkg/blueprint``. Python-side names use
underscores; aliases map them to TOML names where they differ
(``tls_verify`` → ``tls-verify``).

There are two kinds of model:

* **Keyed entries** (:class:`Package`, :class:`User`, :class:`File`, …) live in
  lists that merge by key. ``__key__`` names the key field.
* **Structs** (:class:`Services`, :class:`Firewall`, :class:`Locale`, …) merge
  field by field. ``__merge__`` says how each field merges, and
  ``__exclusive__`` lists groups of string sets an item may only be in one of
  (a service can't be both enabled and masked).
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable
from typing import Any, ClassVar, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .markers import Remove, Replace, UnsetType

__all__ = [
    "AnacondaModules",
    "Container",
    "Directory",
    "EnabledModule",
    "File",
    "Filesystem",
    "Firewall",
    "FirewallServices",
    "FirewallZone",
    "Group",
    "Installer",
    "Kernel",
    "KeyedModel",
    "Kickstart",
    "Locale",
    "Package",
    "PackageGroup",
    "Repository",
    "SSHKey",
    "Services",
    "StructModel",
    "User",
    "parse_size",
]


class _Model(BaseModel):
    """Base for all bpkit models: strict fields, positional-arg shorthands."""

    model_config = ConfigDict(
        extra="forbid",
        validate_by_name=True,
        validate_by_alias=True,
        arbitrary_types_allowed=True,
        validate_default=False,
    )

    #: Field names that may be given positionally, in order.
    __positional__: ClassVar[tuple[str, ...]] = ()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if args:
            cls = type(self).__name__
            if len(args) > len(self.__positional__):
                raise TypeError(
                    f"{cls}() takes at most {len(self.__positional__)} positional "
                    f"argument(s) ({', '.join(self.__positional__) or 'none'}), "
                    f"got {len(args)}"
                )
            for name, value in zip(self.__positional__, args, strict=False):
                if name in kwargs:
                    raise TypeError(f"{cls}() got multiple values for argument {name!r}")
                kwargs[name] = value
        super().__init__(**kwargs)

    def to_blueprint(self) -> dict[str, Any]:
        """Dump in blueprint shape: TOML names, empty/unset fields omitted."""
        return clean(self.model_dump(by_alias=True, exclude_none=True))


def clean(value: Any, *, keep_empty: bool = False) -> Any:
    """Recursively drop ``None`` and (unless ``keep_empty``) empty lists and dicts."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            v = clean(v, keep_empty=keep_empty)
            if v is None or (not keep_empty and (v == [] or v == {})):
                continue
            out[k] = v
        return out
    if isinstance(value, list):
        return [clean(v, keep_empty=keep_empty) for v in value]
    return value


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------

_SIZE_RE = re.compile(r"^(\d+)\s*(kB|KiB|MB|MiB|GB|GiB|TB|TiB)?$")
_SIZE_UNITS = {
    None: 1,
    "kB": 1000,
    "KiB": 1024,
    "MB": 1000**2,
    "MiB": 1024**2,
    "GB": 1000**3,
    "GiB": 1024**3,
    "TB": 1000**4,
    "TiB": 1024**4,
}


def parse_size(size: str | int) -> int:
    """Parse a size the way image-builder does (``"10 GiB"``, ``"512MiB"``, ``1024``)."""
    if isinstance(size, bool):
        raise TypeError("size must be a string or int, not bool")
    if isinstance(size, int):
        if size <= 0:
            raise ValueError(f"size must be positive, got {size}")
        return size
    m = _SIZE_RE.match(size.strip())
    if not m:
        raise ValueError(
            f"invalid size {size!r}: expected a whole number with optional unit "
            "(kB, KiB, MB, MiB, GB, GiB, TB, TiB), e.g. '10 GiB'"
        )
    value = int(m.group(1)) * _SIZE_UNITS[m.group(2)]
    if value <= 0:
        raise ValueError(f"size must be positive, got {size!r}")
    return value


def _abs_path(value: str, what: str = "path") -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise ValueError(f"{what} must be an absolute path, got {value!r}")
    if value != "/" and (posixpath.normpath(value) != value or value.endswith("/")):
        raise ValueError(f"{what} must be a normalised path, got {value!r}")
    return value


def _mode(value: str | int | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("mode must be an octal string like '0644' or an int like 0o644")
    if isinstance(value, int):
        if not 0 <= value <= 0o7777:
            raise ValueError(f"mode out of range: {value:#o}")
        return f"0{value:03o}"
    if not isinstance(value, str) or not re.fullmatch(r"0?[0-7]{3,4}", value):
        raise ValueError(f"mode must be an octal string like '0644', got {value!r}")
    return value if len(value) >= 4 else f"0{value}"


def _str_list(value: Any) -> Any:
    """Accept a single string where a list of strings is expected."""
    if isinstance(value, str):
        return [value]
    return value


# --------------------------------------------------------------------------
# Keyed entries
# --------------------------------------------------------------------------


class KeyedModel(_Model):
    """An entry in a list that merges by key."""

    #: Name of the field that identifies this entry.
    __key__: ClassVar[str] = "name"

    @property
    def merge_key(self) -> Any:
        return getattr(self, self.__key__)

    @classmethod
    def from_shorthand(cls, value: str) -> KeyedModel:
        """Build an entry from a bare string (``"vim"`` → ``Package("vim")``)."""
        return cls(value)


class Package(KeyedModel):
    """An RPM package (also used for ``modules``). ``"vim"`` is shorthand."""

    __positional__ = ("name", "version")

    name: str = Field(min_length=1)
    version: str = "*"


class EnabledModule(KeyedModel):
    """A DNF module stream to enable. ``"nodejs:20"`` is shorthand."""

    __positional__ = ("name", "stream")

    name: str = Field(min_length=1)
    stream: str | None = None

    @classmethod
    def from_shorthand(cls, value: str) -> EnabledModule:
        name, _, stream = value.partition(":")
        return cls(name, stream or None)


class PackageGroup(KeyedModel):
    """A package group (``[[groups]]``). ``"Development Tools"`` is shorthand."""

    __positional__ = ("name",)

    name: str = Field(min_length=1)


class Container(KeyedModel):
    """A container image embedded into the image, keyed by ``source``."""

    __key__ = "source"
    __positional__ = ("source", "name")

    source: str = Field(min_length=1)
    name: str | None = None
    tls_verify: bool | None = Field(default=None, alias="tls-verify")
    local_storage: bool | None = Field(default=None, alias="local-storage")


class SSHKey(KeyedModel):
    """``[[customizations.sshkey]]``, keyed by ``user``."""

    __key__ = "user"
    __positional__ = ("user", "key")

    user: str = Field(min_length=1)
    key: str = Field(min_length=1)


class User(KeyedModel):
    """``[[customizations.user]]``, keyed by ``name``."""

    __positional__ = (
        "name",
        "description",
        "password",
        "key",
        "home",
        "shell",
        "groups",
        "uid",
        "gid",
        "expiredate",
    )

    name: str = Field(min_length=1)
    description: str | None = None
    password: str | None = None
    key: str | None = None
    home: str | None = None
    shell: str | None = None
    groups: list[str] | None = None
    uid: int | None = Field(default=None, ge=0)
    gid: int | None = Field(default=None, ge=0)
    expiredate: int | None = None
    force_password_reset: bool | None = None

    _v_groups = field_validator("groups", mode="before")(_str_list)

    @field_validator("home")
    @classmethod
    def _v_home(cls, v: str | None) -> str | None:
        return None if v is None else _abs_path(v, "home")


class Group(KeyedModel):
    """``[[customizations.group]]`` (a user group), keyed by ``name``."""

    __positional__ = ("name", "gid")

    name: str = Field(min_length=1)
    gid: int | None = Field(default=None, ge=0)


class Filesystem(KeyedModel):
    """``[[customizations.filesystem]]``, keyed by ``mountpoint``.

    ``minsize`` (alias ``size``) is a string with units (``"10 GiB"``) or int
    bytes. Strings are validated and passed through unchanged.
    """

    __key__ = "mountpoint"
    __positional__ = ("mountpoint", "minsize")

    mountpoint: str
    minsize: str | int = Field(validation_alias=AliasChoices("minsize", "size"))

    @field_validator("mountpoint")
    @classmethod
    def _v_mountpoint(cls, v: str) -> str:
        return _abs_path(v, "mountpoint")

    @field_validator("minsize")
    @classmethod
    def _v_minsize(cls, v: str | int) -> str | int:
        parse_size(v)
        return v

    @property
    def size_bytes(self) -> int:
        return parse_size(self.minsize)


class File(KeyedModel):
    """``[[customizations.files]]``, keyed by ``path``."""

    __key__ = "path"
    __positional__ = ("path", "data", "user", "group", "mode")

    path: str
    data: str | None = None
    uri: str | None = None
    user: str | int | None = None
    group: str | int | None = None
    mode: str | None = None

    @field_validator("path")
    @classmethod
    def _v_path(cls, v: str) -> str:
        return _abs_path(v)

    @field_validator("mode", mode="before")
    @classmethod
    def _v_mode(cls, v: Any) -> str | None:
        return _mode(v)

    @model_validator(mode="after")
    def _v_data_uri(self) -> File:
        if self.data is not None and self.uri is not None:
            raise ValueError("File: set either 'data' or 'uri', not both")
        return self


class Directory(KeyedModel):
    """``[[customizations.directories]]``, keyed by ``path``."""

    __key__ = "path"
    __positional__ = ("path", "user", "group", "mode", "ensure_parents")

    path: str
    user: str | int | None = None
    group: str | int | None = None
    mode: str | None = None
    ensure_parents: bool | None = None

    @field_validator("path")
    @classmethod
    def _v_path(cls, v: str) -> str:
        return _abs_path(v)

    @field_validator("mode", mode="before")
    @classmethod
    def _v_mode(cls, v: Any) -> str | None:
        return _mode(v)


class Repository(KeyedModel):
    """``[[customizations.repositories]]``, keyed by ``id``."""

    __key__ = "id"
    __positional__ = ("id", "baseurls")

    id: str = Field(min_length=1)
    baseurls: list[str] | None = None
    gpgkeys: list[str] | None = None
    metalink: str | None = None
    mirrorlist: str | None = None
    name: str | None = None
    priority: int | None = None
    enabled: bool | None = None
    gpgcheck: bool | None = None
    repo_gpgcheck: bool | None = None
    sslverify: bool | None = None
    module_hotfixes: bool | None = None
    filename: str | None = None
    install_from: bool | None = None

    _v_lists = field_validator("baseurls", "gpgkeys", mode="before")(_str_list)

    @model_validator(mode="after")
    def _v_source(self) -> Repository:
        if not (self.baseurls or self.metalink or self.mirrorlist):
            raise ValueError(
                f"Repository {self.id!r}: one of 'baseurls', 'metalink' or 'mirrorlist' is required"
            )
        return self


class FirewallZone(KeyedModel):
    """``[[customizations.firewall.zones]]``, keyed by ``name``."""

    __positional__ = ("name", "sources")

    name: str = Field(min_length=1)
    sources: list[str] | None = None

    _v_sources = field_validator("sources", mode="before")(_str_list)


class Kickstart(_Model):
    """``customizations.installer.kickstart``."""

    __positional__ = ("contents",)

    contents: str


# --------------------------------------------------------------------------
# Structs
# --------------------------------------------------------------------------

#: A string-set field inside a struct: may hold ``Remove`` items, or be
#: wrapped in ``Replace``, or be ``Unset``.
StrSetField = list[str | Remove] | Replace | UnsetType | None


class StructModel(_Model):
    """A struct that merges field by field across inheritance.

    ``__merge__`` maps field → ``"set"`` | ``"scalar"`` | ``"struct"`` |
    ``"keyed"``. ``__exclusive__`` holds groups of set fields that an item may
    only be in one of; the latest declaration wins and moves the item.
    """

    __merge__: ClassVar[dict[str, str]] = {}
    __exclusive__: ClassVar[tuple[tuple[str, ...], ...]] = ()

    @model_validator(mode="before")
    @classmethod
    def _v_str_sets(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for name, kind in cls.__merge__.items():
                if kind == "set" and isinstance(data.get(name), str):
                    data = {**data, name: [data[name]]}
        return data

    @model_validator(mode="after")
    def _v_exclusive(self) -> StructModel:
        for group in self.__exclusive__:
            seen: dict[str, str] = {}
            for fname in group:
                items = getattr(self, fname)
                if not isinstance(items, list):
                    items = items.value if isinstance(items, Replace) else []
                for item in items:
                    if isinstance(item, str):
                        if item in seen and seen[item] != fname:
                            raise ValueError(
                                f"{item!r} is listed in both {seen[item]!r} and {fname!r}"
                            )
                        seen[item] = fname
        return self

    # -- helpers for customize() ------------------------------------------

    def _set_add(self, fname: str, *items: str) -> None:
        """Add items to a set field, moving them out of exclusive siblings."""
        current = getattr(self, fname)
        if not isinstance(current, list):
            current = []
            setattr(self, fname, current)
        for item in items:
            if item not in current:
                current.append(item)
        for group in self.__exclusive__:
            if fname in group:
                for other in group:
                    if other != fname and isinstance(getattr(self, other), list):
                        setattr(self, other, [i for i in getattr(self, other) if i not in items])

    def _set_discard(self, fname: str, *items: str) -> None:
        current = getattr(self, fname)
        if isinstance(current, list):
            setattr(self, fname, [i for i in current if i not in items])

    def is_empty(self) -> bool:
        return not self.to_blueprint()


class Kernel(StructModel):
    """``customizations.kernel`` (``append`` comes from the ``kernel_append`` field)."""

    __merge__ = {"name": "scalar", "version": "scalar"}
    __positional__ = ("name", "version")

    name: str | UnsetType | None = None
    version: str | UnsetType | None = None


class Services(StructModel):
    """``customizations.services``.

    Enabling a service the parent disabled or masked (or the other way round)
    moves it: the latest declaration wins and the service is never listed twice.
    """

    __merge__ = {
        "enabled": "set",
        "disabled": "set",
        "masked": "set",
        "default_target": "scalar",
    }
    __exclusive__ = (("enabled", "disabled", "masked"),)

    enabled: StrSetField = None
    disabled: StrSetField = None
    masked: StrSetField = None
    default_target: str | UnsetType | None = None

    def enable(self, *names: str) -> None:
        self._set_add("enabled", *names)

    def disable(self, *names: str) -> None:
        self._set_add("disabled", *names)

    def mask(self, *names: str) -> None:
        self._set_add("masked", *names)

    def reset(self, *names: str) -> None:
        """Remove services from every list (leave them at the distro default)."""
        for f in ("enabled", "disabled", "masked"):
            self._set_discard(f, *names)


class FirewallServices(StructModel):
    """``customizations.firewall.services``."""

    __merge__ = {"enabled": "set", "disabled": "set"}
    __exclusive__ = (("enabled", "disabled"),)

    enabled: StrSetField = None
    disabled: StrSetField = None

    def enable(self, *names: str) -> None:
        self._set_add("enabled", *names)

    def disable(self, *names: str) -> None:
        self._set_add("disabled", *names)


class Firewall(StructModel):
    """``customizations.firewall``."""

    __merge__ = {"ports": "set", "services": "struct", "zones": "keyed"}

    ports: StrSetField = None
    services: FirewallServices | Replace | UnsetType | None = None
    zones: list[FirewallZone | Remove] | Replace | UnsetType | None = None

    def open_port(self, *ports: str) -> None:
        self._set_add("ports", *ports)

    def close_port(self, *ports: str) -> None:
        self._set_discard("ports", *ports)


class Locale(StructModel):
    """``customizations.locale``."""

    __merge__ = {"languages": "set", "keyboard": "scalar"}
    __positional__ = ("languages", "keyboard")

    languages: StrSetField = None
    keyboard: str | UnsetType | None = None


class AnacondaModules(StructModel):
    """``customizations.installer.modules``."""

    __merge__ = {"enable": "set", "disable": "set"}
    __exclusive__ = (("enable", "disable"),)

    enable: StrSetField = None
    disable: StrSetField = None


class Installer(StructModel):
    """``customizations.installer`` (anaconda-based installer images).

    Less common installer settings (bootloader, payload) can go in
    ``extra_customizations``.
    """

    __merge__ = {
        "unattended": "scalar",
        "sudo_nopasswd": "set",
        "kickstart": "scalar",
        "modules": "struct",
    }

    unattended: bool | UnsetType | None = None
    sudo_nopasswd: StrSetField = Field(default=None, alias="sudo-nopasswd")
    kickstart: Kickstart | UnsetType | None = None
    modules: AnacondaModules | Replace | UnsetType | None = None

    @field_validator("kickstart", mode="before")
    @classmethod
    def _v_kickstart(cls, v: Any) -> Any:
        return Kickstart(v) if isinstance(v, str) else v


#: Allowed ``partitioning_mode`` values.
PartitioningMode = Literal["auto-lvm", "lvm", "raw", "btrfs"]


def keyed_model_for(model: type[StructModel], fname: str) -> type[KeyedModel]:
    """The entry model for a ``"keyed"`` struct field (from its annotation)."""
    ann = model.model_fields[fname].annotation
    for arg in _iter_type_args(ann):
        if isinstance(arg, type) and issubclass(arg, KeyedModel):
            return arg
    raise TypeError(f"{model.__name__}.{fname} has no keyed entry type")  # pragma: no cover


def struct_model_for(model: type[StructModel], fname: str) -> type[StructModel]:
    """The struct model for a ``"struct"`` struct field (from its annotation)."""
    ann = model.model_fields[fname].annotation
    for arg in _iter_type_args(ann):
        if isinstance(arg, type) and issubclass(arg, StructModel):
            return arg
    raise TypeError(f"{model.__name__}.{fname} has no struct type")  # pragma: no cover


def _iter_type_args(tp: Any) -> Iterable[Any]:
    yield tp
    for arg in getattr(tp, "__args__", ()) or ():
        yield from _iter_type_args(arg)
