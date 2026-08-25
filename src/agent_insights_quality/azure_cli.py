from __future__ import annotations

import os
import shutil
from pathlib import Path

from agent_insights_quality.util import ContractError


def azure_cli() -> str:
    resolved = shutil.which("az") or shutil.which("az.cmd")
    if resolved:
        return resolved
    program_files = os.environ.get("ProgramFiles(x86)")
    if program_files:
        candidate = (
            Path(program_files)
            / "Microsoft SDKs"
            / "Azure"
            / "CLI2"
            / "wbin"
            / "az.cmd"
        )
        if candidate.is_file():
            return str(candidate)
    raise ContractError("Azure CLI is not installed or discoverable")
