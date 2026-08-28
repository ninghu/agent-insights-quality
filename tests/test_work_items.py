from __future__ import annotations

import json
import subprocess
from datetime import date
from types import SimpleNamespace

import pytest

from agent_insights_quality.util import ContractError, runtime_root
from agent_insights_quality.work_items import (
    _run_boards_query,
    _closed_items_wiql,
    fetch_quality_work_items,
    load_quality_work_items,
    normalize_quality_work_items,
)


QUERY_URL = (
    "https://synthetic.visualstudio.com/PublicProject/_queries/query/"
    "00000000-0000-0000-0000-000000000001/"
)


def test_boards_query_retries_transient_failures(monkeypatch) -> None:
    attempts = 0

    def run(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise subprocess.TimeoutExpired("az", 120)
        return SimpleNamespace(
            returncode=0,
            stdout="[]",
        )

    monkeypatch.setattr("agent_insights_quality.work_items.subprocess.run", run)
    monkeypatch.setattr("agent_insights_quality.work_items.time.sleep", lambda _: None)
    assert _run_boards_query(["--id", "synthetic"]) == []
    assert attempts == 2


def _item(
    item_id: int,
    *,
    tags: str = "Quality",
    state: str = "Active",
    assigned_to: object = None,
    closed_date: str | None = None,
) -> dict:
    value = {
        "id": item_id,
        "fields": {
            "System.Id": item_id,
            "System.WorkItemType": "Bug",
            "System.Title": f"Synthetic issue {item_id}",
            "System.AssignedTo": assigned_to,
            "System.State": state,
            "System.Tags": tags,
        },
    }
    if closed_date is not None:
        value["fields"]["Microsoft.VSTS.Common.ClosedDate"] = closed_date
    return value


def test_quality_work_items_are_filtered_and_sanitized(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_root = tmp_path / ".aiq-runtime" / "test-runtime"
    monkeypatch.setenv("AIQ_RUNTIME_ROOT", str(runtime_root))
    values = [
        _item(4, assigned_to={"displayName": "Example Owner"}),
        _item(2, state="Completed"),
        _item(3, state="Removed"),
        _item(1, tags="Other"),
        _item(5, tags="Other; quality", assigned_to=None),
        _item(6),
    ]
    del values[-1]["fields"]["System.AssignedTo"]
    items = normalize_quality_work_items(values, QUERY_URL)
    assert items == [
        {
            "id": 4,
            "type": "Bug",
            "title": "Synthetic issue 4",
            "assigned_to": "Example Owner",
            "state": "Active",
            "url": (
                "https://synthetic.visualstudio.com/PublicProject/"
                "_workitems/edit/4"
            ),
        },
        {
            "id": 5,
            "type": "Bug",
            "title": "Synthetic issue 5",
            "assigned_to": "Unassigned",
            "state": "Active",
            "url": (
                "https://synthetic.visualstudio.com/PublicProject/"
                "_workitems/edit/5"
            ),
        },
        {
            "id": 6,
            "type": "Bug",
            "title": "Synthetic issue 6",
            "assigned_to": "Unassigned",
            "state": "Active",
            "url": (
                "https://synthetic.visualstudio.com/PublicProject/"
                "_workitems/edit/6"
            ),
        },
    ]
    snapshot = runtime_root / "work-items.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(
        json.dumps(
            {
                "schema_version": "2.0.0",
                "query_reference": "sha256:" + "a" * 64,
                "closed_business_date": "2026-08-24",
                "active_items": items,
                "closed_yesterday_items": normalize_quality_work_items(
                    [
                        _item(
                            7,
                            state="Closed",
                            closed_date="2026-08-24T18:00:00Z",
                        ),
                        _item(
                            8,
                            state="Closed",
                            closed_date="2026-08-25T18:00:00Z",
                        ),
                    ],
                    QUERY_URL,
                    closed=True,
                    closed_business_date=date(2026, 8, 24),
                ),
            }
        ),
        encoding="utf-8",
    )
    loaded = load_quality_work_items(snapshot)
    assert loaded["active_items"] == items
    assert [item["id"] for item in loaded["closed_yesterday_items"]] == [7]
    with pytest.raises(ContractError, match="does not match the report date"):
        load_quality_work_items(snapshot, report_date=date(2026, 8, 26))
    items[0]["state"] = "Completed"
    snapshot.write_text(
        json.dumps(
            {
                "schema_version": "2.0.0",
                "query_reference": "sha256:" + "a" * 64,
                "closed_business_date": "2026-08-24",
                "active_items": items,
                "closed_yesterday_items": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ContractError, match="contains closed data"):
        load_quality_work_items(snapshot)


def test_work_item_query_fails_closed_for_missing_fields_and_public_output(
    tmp_path,
) -> None:
    value = _item(1)
    del value["fields"]["System.Tags"]
    with pytest.raises(ContractError, match="missing required email columns"):
        normalize_quality_work_items([value], QUERY_URL)
    with pytest.raises(ContractError, match=r"\.aiq-runtime"):
        fetch_quality_work_items(
            QUERY_URL,
            date(2026, 8, 25),
            tmp_path / "work-items.json",
        )
    with pytest.raises(ContractError, match=r"\.aiq-runtime"):
        load_quality_work_items(tmp_path / "work-items.json")
    assert normalize_quality_work_items([], QUERY_URL) == []
    wiql = _closed_items_wiql("PublicProject", date(2026, 8, 24))
    assert "[System.TeamProject] = 'PublicProject'" in wiql
    assert "[Microsoft.VSTS.Common.ClosedDate] >= '2026-08-23'" in wiql
    assert "[Microsoft.VSTS.Common.ClosedDate] < '2026-08-26'" in wiql


def test_runtime_root_is_absolute_durable_and_private(
    tmp_path,
    monkeypatch,
) -> None:
    configured = tmp_path / ".aiq-runtime" / "agent-insights-quality"
    monkeypatch.setenv("AIQ_RUNTIME_ROOT", str(configured))
    assert runtime_root() == configured.resolve()
    monkeypatch.setenv("AIQ_RUNTIME_ROOT", ".aiq-runtime/local")
    with pytest.raises(ContractError, match="must be absolute"):
        runtime_root()
    monkeypatch.setenv("AIQ_RUNTIME_ROOT", str(tmp_path / "not-private"))
    with pytest.raises(ContractError, match=r"under \.aiq-runtime"):
        runtime_root()
