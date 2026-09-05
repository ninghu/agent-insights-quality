import pytest

from agent_insights_quality.azure_regions import location_display_name, regions_match
from agent_insights_quality.errors import QualityError


def test_region_display_comes_from_metadata():
    metadata = [{"name": "swedencentral", "displayName": "Sweden Central"}]
    assert location_display_name("swedencentral", metadata) == "SwedenCentral"
    assert regions_match("Sweden Central", "swedencentral")


@pytest.mark.parametrize(
    "location,metadata,code",
    [
        ("", [], "project_location_missing"),
        ("swedencentral", [], "region_metadata_unavailable"),
        ("swedencentral", [{"name": "swedencentral", "displayName": "invalid/link"}],
         "region_display_invalid"),
    ],
)
def test_region_metadata_never_silently_falls_back(location, metadata, code):
    with pytest.raises(QualityError) as error:
        location_display_name(location, metadata)
    assert error.value.code == code


def test_remote_outcome_is_explicit_and_message_is_safe():
    error = QualityError("remote_timeout", retryable=True, request_accepted=None, status=504)
    assert str(error) == "remote_timeout"
    assert error.request_accepted is None
    assert error.retryable
    with pytest.raises(ValueError):
        QualityError("https://private.invalid/secret")
