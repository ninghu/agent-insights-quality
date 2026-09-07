"""Read-only, private evidence context for human review, never public approval.

No model calls or new judgments. Bind retained assessment output to each frozen
unit before quoting it. Model/card prose is untrusted text, not HTML or an action.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json

from .report_context import ReportContextError
from .results import QualityResult, aggregate_results


def _digest(value) -> str:
    return sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def _text(value) -> str:
    if not isinstance(value, str) or len(value) > 100_000:
        raise ReportContextError("report_review_text_invalid")
    return value


def _citation(citations, steps) -> tuple[str, ...]:
    references = []
    for citation in citations:
        key = (citation["attempt"], citation["step_id"])
        step = steps.get(key)
        if step is None or not set(citation["refs"]) <= set(step["allowed_citation_refs"]):
            raise ReportContextError("report_review_citation_mismatch")
        references.append(
            f"attempt {key[0]}, step {key[1]}: " + ", ".join(citation["refs"])
        )
    return tuple(references)


def _judgments(output, canonical, steps) -> dict:
    from jsonschema import ValidationError, validate
    from .assessment import DAILY_SCHEMA

    try:
        validate(output, DAILY_SCHEMA)
    except ValidationError as error:
        raise ReportContextError("report_review_artifact_invalid") from error
    cards = {card["card_alias"]: card for card in output["cards"]}
    if (
        len(cards) != len(output["cards"]) or cards.keys() != canonical.keys()
        or sorted(item["index"] for item in output["attempts"]) != list(range(1, 11))
    ):
        raise ReportContextError("report_review_result_mismatch")
    for judgment in [*output["cards"], *output["attempts"]]:
        _text(judgment["reason"])
        _citation(judgment["citations"], steps)
    for card in cards.values():
        if (
            (card["core"] == "correct") != (card["root_group"] is not None)
            or card["expected_match"] and card["core"] != "correct"
        ):
            raise ReportContextError("report_review_result_mismatch")
    return cards


def _peers(cards, alias, current) -> set[str]:
    card = cards[alias]
    return {
        other for other in current
        if cards[other]["root_group"] is not None and (
            cards[other]["root_group"] == card["root_group"]
            or cards[other]["expected_match"] and card["expected_match"]
        )
    }


def _disagreed(initial, review, alias, current) -> bool:
    first, second = initial[alias], review[alias]
    return (
        (first["core"], first["expected_match"]) != (second["core"], second["expected_match"])
        or _peers(initial, alias, current) != _peers(review, alias, current)
    )


@dataclass(frozen=True, init=False)
class RetainedReviewContext:
    _result_digest: str
    _units: dict

    def __init__(self, runtime, run_id: str, result: QualityResult) -> None:
        from .runner import restore_unit
        from .report_links import foundry_links

        records = runtime.run(run_id)
        agent_links, _ = foundry_links(runtime, run_id, tuple(unit.planned for unit in result.units))
        units = {}
        for unit in result.units:
            identity = unit.planned.unit_id
            key = f"targets/{identity.agent}/{identity.logical_version}"
            source = records.read(key + "/source", missing_ok=True)
            retained = {"cards": {}, "observations": []}
            if identity.agent in agent_links:
                retained["agent_href"] = agent_links[identity.agent]
            if source and source.get("traffic_run_id") and source.get("work_key"):
                work = runtime.run(source["traffic_run_id"])
                deployment = work.read(source["work_key"] + "/deployment", missing_ok=True)
                if deployment:
                    if deployment.get("target_key") != f"{identity.agent}/{identity.logical_version}":
                        raise ReportContextError("report_review_result_mismatch")
                    retained["deployment"] = {
                        k: deployment[k] for k in ("agent_name", "provider_version", "source_revision")
                    }
                failure = records.read(key + "/failure", missing_ok=True)
                if failure:
                    retained["failure_code"] = failure.get("code")
            reference = source.get("assessment") if source else None
            if not reference:
                units[identity] = {**retained, "unavailable": "Retained assessment reference unavailable."}
                continue
            origin = runtime.run(reference["run_id"])
            artifact = origin.read_artifact(reference["artifact"], missing_ok=True)
            if artifact is None:
                units[identity] = {**retained, "unavailable": "Retained assessment artifact unavailable."}
                continue
            saved = artifact["unit_result"]
            rebuilt = aggregate_results((unit.planned,), (restore_unit(saved),)).units[0]
            if rebuilt != unit:
                raise ReportContextError("report_review_result_mismatch")
            detail = artifact["private_detail"]
            payload, resolved = detail["input"], detail.get("resolved")
            if payload["target"]["unit_id"] != identity.to_dict():
                raise ReportContextError("report_review_result_mismatch")
            steps = {
                (attempt["index"], step["step_id"]): step
                for attempt in payload["attempts"] for step in attempt["steps"]
            }
            value = {
                **retained,
                "artifact": f"runs/{reference['run_id']}/artifacts/{reference['artifact']}.json",
                "sha256": _digest(artifact),
                "tested_at": source.get("tested_at"),
                "assessed_at": source.get("assessed_at"),
                "traffic_source_revision": source.get("traffic_source_revision"),
                "source_revision": source.get("source_revision"),
                "traffic_run_id": source.get("traffic_run_id"),
                "cards": {}, "observations": [], "limitations": [],
                "evidence_scope": {
                    name: {
                        key: snapshot[key] for key in ("observed_at", "query_complete", "gaps")
                        if key in snapshot
                    } for name in ("snapshot", "visible_snapshot")
                    if isinstance(snapshot := payload.get(name), dict)
                },
            }
            if resolved is not None:
                canonical = {card["card_alias"]: card for card in payload["cards"]}
                findings = {finding.card.card_alias: finding for finding in unit.findings}
                if len(canonical) != len(payload["cards"]) or canonical.keys() != findings.keys():
                    raise ReportContextError("report_review_result_mismatch")
                judgments = {
                    stage: _judgments(detail[stage], canonical, steps)
                    for stage in ("initial", "review", "resolved")
                    if detail.get(stage) is not None
                }
                if "initial" in judgments and "review" in judgments:
                    first = {item["index"]: item for item in detail["initial"]["attempts"]}
                    value["attempt_disagreements"] = [
                        item["index"] for item in detail["review"]["attempts"]
                        if (first[item["index"]]["sufficient"], first[item["index"]]["observed"])
                        != (item["sufficient"], item["observed"])
                    ]
                current_aliases = {
                    alias for alias, finding in findings.items()
                    if finding.card.contribution.value == "current"
                }
                for judgment in judgments["resolved"].values():
                    alias = judgment["card_alias"]
                    finding = findings[alias]
                    if (
                        judgment["core"] != finding.card.core.value
                        or judgment["expected_match"] != (
                            finding.card.core.value == "correct"
                            and finding.card.root_cause_alias == unit.planned.expected_issue_alias
                        )
                    ):
                        raise ReportContextError("report_review_result_mismatch")
                    card = canonical[alias]
                    current = card.get("current") or card.get("previous") or {}
                    value["cards"][alias] = {
                        "title": _text(current.get("title", "Untitled retained card")),
                        "claim": _text(current.get("description", "")),
                        "core": judgment["core"],
                        "reason": _text(judgment["reason"]),
                        "citations": _citation(judgment["citations"], steps),
                        "provider_card_id": _text(current["id"]) if "id" in current else None,
                    }
                    if (
                        alias in current_aliases and judgment["core"] == "unknown"
                        and "initial" in judgments and "review" in judgments
                        and _disagreed(judgments["initial"], judgments["review"], alias, current_aliases)
                    ):
                        value["cards"][alias]["disagreement"] = {
                            stage: {
                                "core": judgments[stage][alias]["core"],
                                "expected_match": judgments[stage][alias]["expected_match"],
                                "reason": _text(judgments[stage][alias]["reason"]),
                                "citations": _citation(judgments[stage][alias]["citations"], steps),
                            } for stage in ("initial", "review")
                        }
                for judgment in resolved["attempts"]:
                    if judgment["sufficient"] and judgment["observed"]:
                        value["observations"].append({
                            "reason": _text(judgment["reason"]),
                            "citations": _citation(judgment["citations"], steps),
                        })
                value["limitations"] = list(resolved["limitations"])
            units[identity] = value
        object.__setattr__(self, "_result_digest", _digest(result.to_dict()))
        object.__setattr__(self, "_units", units)

    def for_result(self, result: QualityResult) -> dict:
        if self._result_digest != _digest(result.to_dict()):
            raise ReportContextError("report_review_result_mismatch")
        return deepcopy(self._units)

    def provenance(self) -> dict:
        return {
            f"{identity.agent}/{identity.logical_version}": {
                k: value[k] for k in ("artifact", "sha256", "unavailable") if k in value
            } for identity, value in self._units.items()
        }
