import asyncio
from copy import deepcopy
import json
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_insights_quality.errors import QualityError
from agent_insights_quality.providers.transport import HttpResponse
from agent_insights_quality.work_items import (
    _query_endpoint, fetch_quality_context, render_private_context, render_work_item_html,
    legacy_work_item_email_context, unavailable_context, validate_work_item_context,
)

QUERY = "https://dev.azure.com/synthetic/project/_queries/query/11111111-1111-1111-1111-111111111111"
FRIDAY = "2026-09-04T17:15:00+00:00"
MONDAY = "2026-09-07T17:20:00+00:00"
PREVIOUS = {"delivery_id": "daily-2026-09-04", "cutoff": FRIDAY}


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
        "System.WorkItemType": "Bug", "System.Tags": tags,
    }
    if closed:
        fields["Microsoft.VSTS.Common.ClosedDate"] = closed
    return {"id": index, "fields": fields}


def query(*ids, as_of=MONDAY):
    return {"workItems": [{"id": value} for value in ids], "asOf": as_of, "queryType": "flat"}


def fetch(port, *, previous=PREVIOUS):
    return asyncio.run(fetch_quality_context(
        QUERY, date(2026, 9, 7), port, previous_snapshot=previous,
    ))


def context():
    return {"schema_version": "1.0", "status": "available",
            "snapshot": fetch(Transport(query(1), {"value": [item(1)]}))}


def test_friday_to_monday_keeps_query_scope_quality_tag_and_entire_weekend():
    values = [
        item(1), item(2, tags="quality"), item(3, "Removed"),
        item(4, "Closed", closed=FRIDAY),
        item(5, "Closed", closed="2026-09-05T23:30:00Z"),
        item(6, "Closed", closed="2026-09-06T23:30:00Z"),
        item(7, "Closed", closed=MONDAY),
        item(8, "Closed", closed="2026-09-04T17:14:59Z"),
    ]
    port = Transport(query(*(value["id"] for value in values)), {"value": values})
    result = fetch(port)
    assert [value["id"] for value in result["active"]] == [1]
    assert [value["id"] for value in result["closed"]] == [4, 5, 6]
    assert result["window"] == {
        "start": FRIDAY, "end": MONDAY, "timezone": "America/Los_Angeles",
        "basis": "previous_official_report", "previous_delivery_id": PREVIOUS["delivery_id"],
    }
    params = parse_qs(urlsplit(port.calls[1].url).query)
    assert params["asOf"] == [MONDAY]
    assert "System.WorkItemType" in params["fields"][0]
    assert len(port.calls) == 2
    assert "/wiql/11111111-1111-1111-1111-111111111111?" in port.calls[0].url
    text = render_private_context(result)
    assert "Synthetic item 1" in text and "Synthetic item 4" in text
    assert "Synthetic item 3" not in text and "Bug" in text
    assert "2026-09-04 10:15:00-07:00 (inclusive)" in text
    assert "2026-09-07 10:20:00-07:00 (exclusive), America/Los_Angeles" in text
    assert "previous successfully submitted official report snapshot" in text


def test_no_history_has_explicit_bounded_initial_lookback_not_previous_calendar_day():
    result = fetch(Transport(query()), previous=None)
    assert result["window"]["start"] == "2026-08-31T17:20:00+00:00"
    assert result["window"]["basis"] == "initial_lookback"
    html = render_work_item_html({"schema_version": "1.0", "status": "available", "snapshot": result})
    assert "Initial 7-day lookback" in html and "no prior successfully submitted official snapshot" in html
    assert "None in this snapshot." in html
    assert "previous successfully submitted official report snapshot" not in html


def test_daylight_saving_offsets_are_explicit_and_window_is_half_open():
    previous = {"delivery_id": "daily-2026-10-30", "cutoff": "2026-10-30T17:00:00+00:00"}
    end = "2026-11-02T18:00:00+00:00"
    values = [
        item(1, "Closed", closed="2026-11-01T01:30:00-07:00"),
        item(2, "Closed", closed="2026-11-01T01:30:00-08:00"),
    ]
    snapshot = fetch(Transport(query(1, 2, as_of=end), {"value": values}), previous=previous)
    text = render_private_context(snapshot)
    assert len(snapshot["closed"]) == 2
    assert "2026-10-30 10:00:00-07:00" in text and "2026-11-02 10:00:00-08:00" in text


def test_provider_clock_cutoff_is_used_for_all_batches_even_if_clock_advances():
    now = datetime(2026, 9, 7, 17, 20, tzinfo=timezone.utc)
    ids = list(range(1, 202))
    class ClockTransport:
        def __init__(self):
            self.calls = []
        async def send(self, request):
            nonlocal now
            self.calls.append(request)
            if len(self.calls) == 1:
                response = query(*ids, as_of=now.isoformat())
            else:
                params = parse_qs(urlsplit(request.url).query)
                assert params["asOf"] == [MONDAY]
                response = {"value": [item(int(value)) for value in params["ids"][0].split(",")]}
            now += timedelta(hours=1)
            return HttpResponse(200, {}, json.dumps(response).encode())
    port = ClockTransport()
    result = fetch(port)
    assert result["window"]["end"] == MONDAY and len(port.calls) == 3


@pytest.mark.parametrize("url", [
    "http://dev.azure.com/org/project/_queries/query/x",
    "https://external.invalid/project/_queries/query/x",
    "https://user@dev.azure.com/org/project/_queries/query/x",
    "https://dev.azure.com/org/project/_queries/query/not-an-id",
    "https://dev.azure.com:invalid/org/project/_queries/query/x",
    QUERY.replace("dev.azure.com", "dev.azure.com:0"),
    QUERY.replace("dev.azure.com", "@dev.azure.com"),
    "https://dev.azure.com/org/project%2Finjected/_queries/query/x",
    QUERY + "?redirect=unsafe",
    QUERY + "#fragment",
    QUERY.replace("/project/", "/project%00/"),
    None,
])
def test_private_query_does_not_accept_arbitrary_token_destinations(url):
    with pytest.raises(QualityError, match="work_item_query_invalid"):
        _query_endpoint(url)


def test_missing_requested_work_item_is_not_a_complete_snapshot():
    port = Transport(query(1, 2), {"value": [item(1)]})
    with pytest.raises(QualityError, match="work_item_response_incomplete"):
        fetch(port)


def test_failed_quality_query_is_unavailable_not_an_empty_snapshot():
    class Rejected:
        async def send(self, request):
            return HttpResponse(503, {}, b'{"workItems":[]}')
    with pytest.raises(QualityError, match="work_item_unavailable"):
        fetch(Rejected())


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
    port = Transport(query(1, 2), {"value": values})
    with pytest.raises(QualityError, match="work_item_response_incomplete"):
        fetch(port)


@pytest.mark.parametrize("field,value", [
    ("System.AssignedTo", {"displayName": {"unsafe": "object"}}),
    ("System.AssignedTo", "unknown identity shape"),
    ("System.AssignedTo", {}),
    ("System.WorkItemType", None),
    ("System.WorkItemType", ""),
    ("System.WorkItemType", ["Bug"]),
    ("System.Title", {"private": "object"}),
    ("System.State", 1),
])
def test_malformed_private_fields_are_not_rendered(field, value):
    work_item = item(1)
    work_item["fields"][field] = value
    port = Transport(query(1), {"value": [work_item]})
    with pytest.raises(QualityError, match="work_item_response_invalid"):
        fetch(port)


@pytest.mark.parametrize("as_of", [None, "", "yesterday", "2026-09-07T17:20:00", {"date": MONDAY}])
def test_missing_or_invalid_provider_snapshot_cutoff_is_not_guessed(as_of):
    with pytest.raises(QualityError, match="work_item_snapshot_cutoff_missing"):
        fetch(Transport(query(as_of=as_of)))


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "https://external.invalid/_workitems/edit/1",
    "https://dev.azure.com/other/project/_workitems/edit/1",
    "https://dev.azure.com/synthetic/other/_workitems/edit/1",
    "https://dev.azure.com/synthetic/project/_workitems/edit/2",
    "https://dev.azure.com/synthetic/project/_workitems/edit/1?redirect=unsafe",
    "https://dev.azure.com/synthetic/project/_workitems/edit/1#fragment",
    "https://dev.azure.com/synthetic/project/_workitems/edit/1/extra",
    None,
])
def test_render_rejects_non_item_or_wrong_scope_links(url):
    value = context()
    value["snapshot"]["active"][0]["url"] = url
    with pytest.raises(QualityError, match="work_item_context_invalid"):
        render_work_item_html(value)


@pytest.mark.parametrize("field,value", [
    ("id", True), ("type", None), ("title", []), ("state", "Closed"), ("owner", {}),
])
def test_render_validates_entry_types_before_escaping(field, value):
    document = context()
    document["snapshot"]["active"][0][field] = value
    with pytest.raises(QualityError, match="work_item_context_invalid"):
        render_work_item_html(document)


def test_render_is_escaped_typed_outlook_safe_and_does_not_mutate_snapshot():
    value = context()
    value["snapshot"]["active"][0].update(
        title='<img src=x onerror="unsafe">', type="<Bug>", state="Active & ready", owner="A < B",
    )
    before = deepcopy(value)
    html = render_work_item_html(value)
    assert value == before
    assert "<img" not in html and "&lt;img" in html
    assert "&lt;Bug&gt;" in html and "Active &amp; ready" in html and "A &lt; B" in html
    assert all(f">{column}</th>" in html for column in ("ID", "Type", "Title", "State", "Owner"))
    assert all(value in html for value in ("Segoe UI,Arial", "14px", "#12304a", "#d6deea", "#e8eef7"))
    assert "display:flex" not in html and "<style" not in html
    assert html.startswith("<h2 ")
    assert html.count("<table ") == 1
    assert 'role="presentation"' not in html
    assert not html.endswith("</td></tr></table>")


def test_missing_optional_context_omits_the_section():
    assert render_work_item_html(None) == ""


def test_unavailable_is_distinct_from_empty_success():
    html = render_work_item_html(unavailable_context("work_item_unavailable"))
    assert "Unavailable" in html and "not an empty result" in html
    assert "None" not in html
    assert html.startswith("<h2 ") and "<tr" not in html and "<td" not in html


def test_legacy_snapshot_is_explicitly_unavailable_without_invented_type_or_window():
    html = render_work_item_html(unavailable_context("work_item_legacy_snapshot"))
    assert "Unavailable" in html and "historical snapshot has no typed reporting window" in html
    assert "Type</th>" not in html


def test_strict_schema_rejects_extra_fields_unknown_timezones_and_ambiguous_intervals():
    value = context()
    bad_values = []
    bad = deepcopy(value)
    bad["raw"] = "must not be rendered"
    bad_values.append(bad)
    bad = deepcopy(value)
    bad["snapshot"]["active"][0]["model_output"] = "unexpected"
    bad_values.append(bad)
    for key, malformed in (
        ("timezone", "UTC"), ("start", MONDAY), ("end", "2026-09-07"),
        ("previous_delivery_id", None), ("basis", "yesterday"),
    ):
        bad = deepcopy(value)
        bad["snapshot"]["window"][key] = malformed
        bad_values.append(bad)
    for bad in bad_values:
        with pytest.raises(QualityError):
            validate_work_item_context(bad)


def legacy_checkpoint():
    entry = {key: value for key, value in context()["snapshot"]["active"][0].items() if key != "type"}
    return {
        "status": "available", "text": "Exact retained work-item list\nwith synthetic content",
        "snapshot": {
            "report_date": "2026-09-07", "closed_on": "2026-09-06",
            "active": [entry],
            "closed": [{
                **entry, "id": 2, "state": "Closed", "title": "Synthetic closed item",
                "url": "https://dev.azure.com/synthetic/project/_workitems/edit/2",
            }],
        },
    }


def test_explicit_legacy_table_uses_only_retained_fields_and_discloses_missing_type_and_window():
    checkpoint = legacy_checkpoint()
    before = deepcopy(checkpoint)
    suffix = "\n\nConfigured assessment (intent)\nSynthetic assessor notes"
    value, private_text = legacy_work_item_email_context(checkpoint, checkpoint["text"] + suffix)
    assert checkpoint == before
    assert private_text == suffix
    assert value == {"schema_version": "legacy-1.0", "status": "available", "snapshot": checkpoint["snapshot"]}
    assert "type" not in value["snapshot"]["active"][0] and "window" not in value["snapshot"]
    html = render_work_item_html(value)
    assert html.startswith("<h2 ") and "Closed on 2026-09-06" in html
    assert html.count("Not recorded") == 2
    assert "America/Los_Angeles calendar date" in html and "No refresh was performed" in html
    assert "since-previous-report window were not recorded" in html
    assert "Initial 7-day lookback" not in html and "(inclusive)" not in html
    assert "Exact retained work-item list" not in html
    checkpoint["snapshot"]["active"][0]["title"] = "Changed after normalization"
    assert value["snapshot"]["active"][0]["title"] != "Changed after normalization"


@pytest.mark.parametrize("private_text", [
    None, "Assessor intent only", "Preface\nExact retained work-item list\nwith synthetic content",
    "Exact retained work-item list\nwith different content",
])
def test_legacy_helper_never_parses_prose_or_removes_nonmatching_prefix(private_text):
    checkpoint = legacy_checkpoint()
    _, remaining = legacy_work_item_email_context(checkpoint, private_text)
    assert remaining == private_text


def test_legacy_exact_work_item_text_alone_leaves_no_duplicate_private_prose():
    checkpoint = legacy_checkpoint()
    _, remaining = legacy_work_item_email_context(checkpoint, checkpoint["text"])
    assert remaining is None


@pytest.mark.parametrize("malformation", ["extra-type", "missing-title", "bad-date", "external-url", "mixed-scope"])
def test_legacy_table_rejects_malformed_retained_fields_and_links(malformation):
    checkpoint = legacy_checkpoint()
    snapshot = checkpoint["snapshot"]
    if malformation == "extra-type":
        snapshot["active"][0]["type"] = "Not recorded"
    elif malformation == "missing-title":
        del snapshot["active"][0]["title"]
    elif malformation == "bad-date":
        snapshot["closed_on"] = "Unknown"
    elif malformation == "external-url":
        snapshot["active"][0]["url"] = "https://external.invalid/_workitems/edit/1"
    elif malformation == "mixed-scope":
        snapshot["closed"][0]["url"] = "https://dev.azure.com/other/project/_workitems/edit/2"
    with pytest.raises(QualityError):
        legacy_work_item_email_context(checkpoint, checkpoint["text"])


def test_legacy_table_escapes_actual_titles_owners_and_states():
    checkpoint = legacy_checkpoint()
    checkpoint["snapshot"]["active"][0].update(
        title="<script>synthetic</script>", owner="Synthetic <owner>", state="Active & reviewed",
    )
    value, _ = legacy_work_item_email_context(checkpoint, None)
    html = render_work_item_html(value)
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "Synthetic &lt;owner&gt;" in html and "Active &amp; reviewed" in html


def test_retained_unavailable_stays_unavailable_and_preserves_assessor_text():
    checkpoint = {"status": "unavailable", "code": "work_item_unavailable", "text": None}
    value, private_text = legacy_work_item_email_context(checkpoint, "Synthetic assessor notes")
    assert value == unavailable_context("work_item_unavailable")
    assert private_text == "Synthetic assessor notes"
    assert "Unavailable" in render_work_item_html(value)
