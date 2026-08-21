from __future__ import annotations

import re

from agent_insights_quality.contracts import ROOT


def test_storage_account_name_stays_within_azure_limit() -> None:
    main = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")
    persistent = (ROOT / "infra" / "modules" / "persistent.bicep").read_text(
        encoding="utf-8"
    )
    assert re.search(r"@maxLength\(12\)\s+param uniqueSuffix string", main)
    assert re.search(r"@maxLength\(12\)\s+param uniqueSuffix string", persistent)
    prefix = re.search(r"var storageName = '([^']+)\$\{uniqueSuffix\}'", persistent)
    assert prefix is not None
    assert len(prefix.group(1)) + 12 <= 24
