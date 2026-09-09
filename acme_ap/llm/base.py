"""The provider contract.

Agents depend on this Protocol, never on a vendor SDK. That buys three things
that matter more than they look: the whole system is testable with no network,
a different model is a config change rather than a refactor, and when the API is
unreachable there is somewhere well-defined to fall back to.
"""

from __future__ import annotations

from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """Provider call failed after exhausting retries."""


@runtime_checkable
class LLMClient(Protocol):
    """What every provider must offer."""

    name: str
    model: str

    def complete_structured(self, *, system: str, user: str, schema: type[T], purpose: str) -> T:
        """Return an instance of ``schema``, or raise :class:`LLMError`."""
        ...
