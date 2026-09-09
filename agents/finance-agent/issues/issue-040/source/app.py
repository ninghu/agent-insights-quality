from __future__ import annotations

from .finance import create_agent, run
from .retry import ExactTransientRetry, FinanceRequestScope


def build_agent(*, client=None):
    return create_agent([FinanceRequestScope(), ExactTransientRetry()], client=client)


if __name__ == "__main__":
    run(build_agent())
