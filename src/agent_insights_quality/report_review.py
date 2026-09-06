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


@dataclass(frozen=True, init=False)
class RetainedReviewContext:
    _result_digest: str
    _units: dict

    def __init__(self, runtime, run_id: str, result: QualityResult) -> None:
        from .runner import restore_unit

        records = runtime.run(run_id)
        units = {}
        for unit in result.units:
            identity = unit.planned.unit_id
            key = f"targets/{identity.agent}/{identity.logical_version}"
            source = records.read(key + "/source", missing_ok=True)
            retained = {"cards": {}, "observations": []}
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
                from jsonschema import ValidationError, validate
                from .assessment import DAILY_SCHEMA
                try:
                    validate(resolved, DAILY_SCHEMA)
                except ValidationError as error:
                    raise ReportContextError("report_review_artifact_invalid") from error
                canonical = {card["card_alias"]: card for card in payload["cards"]}
                for judgment in resolved["cards"]:
                    alias = judgment["card_alias"]
                    finding = next((f for f in unit.findings if f.card.card_alias == alias), None)
                    if finding is None or alias not in canonical or judgment["core"] != finding.card.core.value:
                        raise ReportContextError("report_review_result_mismatch")
                    card = canonical[alias]
                    current = card.get("current") or card.get("previous") or {}
                    value["cards"][alias] = {
                        "title": _text(current.get("title", "Untitled retained card")),
                        "claim": _text(current.get("description", "")),
                        "reason": _text(judgment["reason"]),
                        "citations": _citation(judgment["citations"], steps),
                        "provider_card_id": _text(current["id"]) if "id" in current else None,
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
