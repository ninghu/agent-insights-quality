from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class RuntimeFailure(RuntimeError):
    """A fail-closed runtime error containing only public-safe diagnostics."""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    transient: bool = False

    def __post_init__(self) -> None:
        RuntimeError.__init__(self, self.message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "transient": self.transient,
        }
