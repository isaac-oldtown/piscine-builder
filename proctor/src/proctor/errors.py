"""The error every fallible operation raises in place of Rust's
`Result<T, String>`: its message is the `Err` string. `Ok` and `Err` hold a
result that is stored rather than propagated (a field, a list item)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


class Error(Exception):
    """A failure with a message for the user, as Rust's `Err(String)`."""

    def __init__(self, msg: str) -> None:
        super().__init__(msg)
        self.msg = msg

    def __str__(self) -> str:
        return self.msg


@dataclass(frozen=True)
class Ok(Generic[T]):
    value: T


@dataclass(frozen=True)
class Err:
    msg: str


# A stored `Result<T, String>`: match with `case Ok(v)` / `case Err(msg)`.
Result = Ok[T] | Err
