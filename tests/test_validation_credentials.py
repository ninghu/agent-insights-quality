from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_insights_quality.util import ContractError
from agent_insights_quality.validation_credentials import (
    verify_azure_service_principal,
)


def test_azure_federated_identity_must_match_expected_client(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent_insights_quality.validation_credentials.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "user": {
                        "type": "servicePrincipal",
                        "name": "synthetic-client-id",
                    }
                }
            ),
        ),
    )
    verify_azure_service_principal("synthetic-client-id")
    with pytest.raises(ContractError, match="does not match"):
        verify_azure_service_principal("different-client-id")
