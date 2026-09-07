from collections.abc import Callable
from typing import TypeVar

from agent_insights_quality.errors import QualityError

T = TypeVar("T")


def safe_persist(callback: Callable[[T], None]) -> Callable[[T], None]:
    def save(value: T) -> None:
        try:
            callback(value)
        except OSError:
            raise QualityError(
                "provider_checkpoint_failed", request_accepted=None
            ) from None

    return save
