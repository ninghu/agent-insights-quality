import asyncio
import json
from datetime import date

import pytest

from agent_insights_quality.errors import QualityError
from agent_insights_quality.providers.transport import HttpResponse
from agent_insights_quality.work_items import (
    _query_endpoint, fetch_quality_context, render_private_context,
)

QUERY = "https://dev.azure.com/synthetic/project/_queries/query/11111111-1111-1111-1111-111111111111"


class Transport:
    def __init__(self, *values):
        self.values = list(values)
        self.calls = []

    async def send(self, request):
        self.calls.append(request)
        return HttpResponse(200, {}, json.dumps(self.values.pop(0)).encode())


def item(index, state="Active", tags="Quality", closed=None):
    fields = {
        "System.Title": f"Synthetic item {index}", "System.State": state,
        "System.Tags": tags,
    }
    if closed:
        fields["Microsoft.VSTS.Common.ClosedDate"] = closed
    return {"id": index, "fields": fields}


def test_private_context_filters_exact_tag_removed_and_calendar_date():
    values = [
        item(1), item(2, tags="quality"), item(3, "Removed"),
        item(4, "Closed", closed="2026-09-03T12:00:00Z"),
        item(5, "Closed", closed="2026-09-01T12:00:00Z"),
    ]
    port = Transport({"workItems": [{"id": value["id"]} for value in values]}, {"value": values})
    result = asyncio.run(fetch_quality_context(QUERY, date(2026, 9, 4), port))
    assert [value["id"] for value in result["active"]] == [1]
    assert [value["id"] for value in result["closed"]] == [4]
    assert result["closed_on"] == "2026-09-03"
    text = render_private_context(result)
    assert "Synthetic item 1" in text and "Synthetic item 4" in text
    assert "Synthetic item 3" not in text


@pytest.mark.parametrize("url", [
    "http://dev.azure.com/org/project/_queries/query/x",
    "https://external.invalid/project/_queries/query/x",
    "https://user@dev.azure.com/org/project/_queries/query/x",
    "https://dev.azure.com/org/project/_queries/query/not-an-id",
    "https://dev.azure.com:invalid/org/project/_queries/query/x",
    "https://dev.azure.com/org/project%2Finjected/_queries/query/x",
    None,
])
def test_private_query_does_not_accept_arbitrary_token_destinations(url):
    with pytest.raises(QualityError, match="work_item_query_invalid"):
        _query_endpoint(url)


def test_missing_requested_work_item_is_not_a_complete_snapshot():
    port = Transport({"workItems": [{"id": 1}, {"id": 2}]}, {"value": [item(1)]})
    with pytest.raises(QualityError, match="work_item_response_incomplete"):
        asyncio.run(fetch_quality_context(QUERY, date(2026, 9, 4), port))


def test_encoded_project_name_is_not_double_encoded():
    endpoint = _query_endpoint(QUERY.replace("/project/", "/project%20name/"))[1]
    assert "/project%20name/_apis/" in endpoint


@pytest.mark.parametrize("values", [
    [item(True), item(2)],
    [item(1), item(1)],
    [item(1), {"id": [], "fields": {}}],
    [item(1), None],
])
def test_batch_requires_exact_integer_identities(values):
    port = Transport({"workItems": [{"id": 1}, {"id": 2}]}, {"value": values})
    with pytest.raises(QualityError, match="work_item_response_incomplete"):
        asyncio.run(fetch_quality_context(QUERY, date(2026, 9, 4), port))


def test_malformed_private_display_name_is_not_rendered():
    value = item(1)
    value["fields"]["System.AssignedTo"] = {"displayName": {"unsafe": "object"}}
    port = Transport({"workItems": [{"id": 1}]}, {"value": [value]})
    with pytest.raises(QualityError, match="work_item_response_invalid"):
        asyncio.run(fetch_quality_context(QUERY, date(2026, 9, 4), port))
