from __future__ import annotations

import re


class QualityError(RuntimeError):
    """A public-safe failure code with explicit remote-side-effect semantics."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        request_accepted: bool | None = None,
        status: int | None = None,
    ) -> None:
        if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code):
            raise ValueError("Quality error codes must be public-safe identifiers")
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.request_accepted = request_accepted
        self.status = status
