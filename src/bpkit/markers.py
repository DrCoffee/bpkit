"""Merge markers: ``Remove``, ``Replace`` and ``Unset``.

Markers tell the merge engine how a class's declaration relates to what it
inherits. They are always resolved away: none survive into ``to_dict()``.
"""

from __future__ import annotations

from typing import Any, Final, final

__all__ = ["Remove", "Replace", "Unset", "UnsetType"]


@final
class Remove:
    """Drop inherited entries by key from a list field.

    ``packages = ["htop", Remove("tmux")]`` adds ``htop`` and drops the
    inherited ``tmux``. Removing a key that isn't inherited raises, which
    catches typos.
    """

    __slots__ = ("keys",)

    def __init__(self, *keys: Any) -> None:
        if not keys:
            raise TypeError("Remove() needs at least one key")
        self.keys: tuple[Any, ...] = keys

    def __repr__(self) -> str:
        return f"Remove({', '.join(map(repr, self.keys))})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Remove) and other.keys == self.keys

    def __hash__(self) -> int:
        return hash(("Remove", self.keys))


@final
class Replace:
    """Wrap a whole field value to discard everything inherited for that field.

    ``packages = Replace(["nginx"])`` → only ``nginx``.
    """

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        if isinstance(value, (Replace, UnsetType)):
            raise TypeError(f"Replace() cannot wrap {value!r}")
        self.value = value

    def __repr__(self) -> str:
        return f"Replace({self.value!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Replace) and other.value == self.value

    def __hash__(self) -> int:
        return id(self)


@final
class UnsetType:
    """Type of the :data:`Unset` singleton."""

    _instance: UnsetType | None = None

    def __new__(cls) -> UnsetType:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "Unset"

    def __bool__(self) -> bool:
        return False

    def __copy__(self) -> UnsetType:
        return self

    def __deepcopy__(self, memo: Any) -> UnsetType:
        return self

    def __reduce__(self) -> str:
        return "Unset"


#: Remove an inherited value so it is omitted from the output.
Unset: Final = UnsetType()
