"""Verified presentation links. Private Foundry identities never enter public reports."""

from __future__ import annotations

from base64 import urlsafe_b64encode
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re
from urllib.request import urlopen
from uuid import UUID

from .report_context import ReportContextError


REPOSITORY = "ninghu/agent-insights-quality"
_REVISION = r"[0-9a-f]{40}"
_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._()~-]{0,127}"


def _public_read(url: str) -> bytes:
    with urlopen(url, timeout=10) as response:
        # Never follow a redirect to a different host or mutable document.
        if response.geturl() != url:
            raise ReportContextError("report_scoring_link_unverified")
        content = response.read(250_001)
        if len(content) > 250_000:
            raise ReportContextError("report_scoring_link_unverified")
        return content


@dataclass(frozen=True, init=False)
class VerifiedScoringLink:
    href: str
    revision: str
    content_sha256: str

    def __init__(self, root: Path, revision: str, *, fetch=_public_read) -> None:
        if not isinstance(revision, str) or re.fullmatch(_REVISION, revision) is None:
            raise ReportContextError("report_scoring_revision_invalid")
        path = "docs/QUALITY_BAR.md"
        expected = (root / "docs" / "QUALITY_BAR.md").read_bytes().replace(b"\r\n", b"\n")
        actual = fetch(f"https://raw.githubusercontent.com/{REPOSITORY}/{revision}/{path}")
        if not isinstance(actual, bytes) or actual.replace(b"\r\n", b"\n") != expected:
            raise ReportContextError("report_scoring_link_content_mismatch")
        object.__setattr__(
            self, "href", f"https://github.com/{REPOSITORY}/blob/{revision}/{path}#score-and-coverage",
        )
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "content_sha256", sha256(expected).hexdigest())

    def to_dict(self) -> dict:
        return dict(href=self.href, revision=self.revision, content_sha256=self.content_sha256)

    @classmethod
    def from_retained(cls, root: Path, receipt: dict) -> VerifiedScoringLink:
        """Restore only a receipt previously frozen by the trusted delivery boundary."""
        if not isinstance(receipt, dict) or set(receipt) != {"href", "revision", "content_sha256"}:
            raise ReportContextError("report_scoring_link_unverified")
        content = (root / "docs" / "QUALITY_BAR.md").read_bytes().replace(b"\r\n", b"\n")
        if sha256(content).hexdigest() != receipt["content_sha256"]:
            raise ReportContextError("report_scoring_link_content_mismatch")
        link = cls(root, receipt["revision"], fetch=lambda _: content)
        if link.to_dict() != receipt:
            raise ReportContextError("report_scoring_link_unverified")
        return link


def configured_scoring_link(runtime, root: Path) -> VerifiedScoringLink | None:
    from .state import _inside, _read

    value = _read(_inside(runtime.root, runtime.root / "config" / "report-links.json"), missing_ok=True)
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"scoring_revision"}:
        raise ReportContextError("report_scoring_configuration_invalid")
    return VerifiedScoringLink(root, value["scoring_revision"])


def validate_foundry_link(href: str) -> str:
    if not isinstance(href, str) or re.fullmatch(
        rf"https://ai\.azure\.com/nextgen/r/[A-Za-z0-9_-]{{22}},"
        rf"{_SEGMENT},,{_SEGMENT},{_SEGMENT}/build/agents/{_SEGMENT}/build",
        href,
    ) is None:
        raise ReportContextError("report_foundry_link_invalid")
    return href


def foundry_links(runtime, run_id: str, plan) -> tuple[dict[str, str], tuple[str, ...]]:
    """Use the saved Daily environment and actual per-version deployment objects.

    The route (including UUID byte order) comes from the historical renderer,
    bf0de27f:email.py::build_runtime_links. Bootstrap discovers the account and
    g30 telemetry in the same resource group/subscription. No tenant/auth lookup:
    the portal uses the reader's signed-in tenant, not a guessed tenant parameter.
    """
    from .bootstrap import ACCOUNTS, RESOURCE_GROUP

    records = runtime.run(run_id)
    env = records.read_completed("environment", missing_ok=True)
    agents = dict.fromkeys(unit.unit_id.agent for unit in plan)
    missing = tuple("foundry_link_missing:" + agent for agent in agents)
    if not env:
        return {}, missing
    match = re.fullmatch(
        rf"/subscriptions/([0-9a-fA-F-]{{36}})/resourceGroups/({_SEGMENT})/"
        rf"providers/Microsoft\.Insights/components/{_SEGMENT}",
        str(env.get("application_insights_resource_id", "")), re.I,
    )
    if (
        not match or match[2] != RESOURCE_GROUP or env.get("profile") != "daily"
        or env.get("location", "").casefold() != "swedencentral"
        or env.get("account_name") != ACCOUNTS["daily"]
        or env.get("project_name") != ACCOUNTS["daily"]
        or env.get("project_endpoint") != (
            f"https://{ACCOUNTS['daily']}.services.ai.azure.com/api/projects/{ACCOUNTS['daily']}"
        )
    ):
        return {}, missing
    try:
        token = urlsafe_b64encode(UUID(match[1]).bytes).decode("ascii").rstrip("=")
    except ValueError:
        return {}, missing
    route = (
        f"https://ai.azure.com/nextgen/r/{token},{match[2]},,"
        f"{env['account_name']},{env['project_name']}/build/agents/"
    )
    output = {}
    for agent in agents:
        names = set()
        complete = True
        for unit in (unit for unit in plan if unit.unit_id.agent == agent):
            key = f"targets/{agent}/{unit.unit_id.logical_version}"
            source = records.read(key + "/source", missing_ok=True)
            if not source or not source.get("work_key") or not source.get("traffic_run_id"):
                complete = False
                break
            deployment = runtime.run(source["traffic_run_id"]).read(
                source["work_key"] + "/deployment", missing_ok=True,
            )
            if (
                not deployment or deployment.get("target_key") != f"{agent}/{unit.unit_id.logical_version}"
                or not isinstance(deployment.get("provider_version"), str)
                or not re.fullmatch(_SEGMENT, str(deployment.get("agent_name", "")))
            ):
                complete = False
                break
            names.add(deployment["agent_name"])
        if complete and len(names) == 1:
            output[agent] = validate_foundry_link(route + names.pop() + "/build")
    return output, tuple("foundry_link_missing:" + agent for agent in agents if agent not in output)
