from __future__ import annotations

from copy import deepcopy

import pytest

from agent_insights_quality.util import ROOT, ContractError, read_json
from agent_insights_quality.validation_rules import (
    CONVERSATION_PLACEHOLDER,
    RUNTIME_AGENT_NAME_PLACEHOLDER,
    RUNTIME_AGENT_VERSION_PLACEHOLDER,
    stamp_execution_digests,
    validate_validation_rules,
    validation_matrix,
)


def _step(step_id: str, text: str, *, assertions: bool) -> dict:
    return {
        "id": step_id,
        "request": {
            "method": "POST",
            "path": "/responses",
            "headers": {"content-type": "application/json"},
            "body": {
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": text}],
                    }
                ],
                "conversation": {"id": CONVERSATION_PLACEHOLDER},
                "max_output_tokens": 200,
            },
        },
        "expected": {
            "http_status": 200,
            "semantic_assertions": (
                {"required_terms_all": ["synthetic"]} if assertions else {}
            ),
            "trace_assertions": [],
            "identity_assertions": {
                "agent_name": RUNTIME_AGENT_NAME_PLACEHOLDER,
                "agent_version": RUNTIME_AGENT_VERSION_PLACEHOLDER,
            },
        },
    }


def _rules(mode: str = "deterministic") -> dict:
    n, k = validation_matrix(mode)
    scenario = {
        "id": "synthetic-path",
        "validation_mode": mode,
        "n": n,
        "k": k,
        "fixtures": [],
        "attempts": [
            {
                "index": index,
                "conversation_group": f"synthetic-{index}",
                "parameters": {"case": index},
                "setup_steps": [
                    _step(
                        f"setup-{index}",
                        f"Acknowledge synthetic setup {index}.",
                        assertions=False,
                    )
                ],
                "probe_steps": [
                    _step(
                        f"probe-{index}",
                        f"Evaluate synthetic case {index}.",
                        assertions=True,
                    )
                ],
            }
            for index in range(1, n + 1)
        ],
        "healthy_predicate": (
            {"kind": "all_probe_assertions_pass"} if mode == "baseline" else None
        ),
        "defect_predicate": (
            {"kind": "never"}
            if mode == "baseline"
            else {
                "kind": "all_observation_steps_pass",
                "step_ids": [f"probe-{index}" for index in range(1, n + 1)],
                "required_surfaces": ["semantic"],
            }
        ),
        "v0_control_predicate": (
            None if mode == "baseline" else {"kind": "zero_defect_observations"}
        ),
    }
    return stamp_execution_digests(
        {
            "schema_version": "1.0.0",
            "scenarios": [scenario],
        },
        authority_id="issue-999" if mode != "baseline" else "synthetic-agent/v0",
        authority_kind="issue" if mode != "baseline" else "baseline",
        canonical_agent="synthetic-agent",
        logical_version="issue-999" if mode != "baseline" else "v0",
        runtime_kind="hosted_code",
        framework="synthetic_framework",
        model_contract={"id": "synthetic-model", "version": "1"},
    )


def _validate(rules: dict, mode: str = "deterministic") -> None:
    validate_validation_rules(
        rules,
        authority_id="issue-999" if mode != "baseline" else "synthetic-agent/v0",
        authority_kind="issue" if mode != "baseline" else "baseline",
        canonical_agent="synthetic-agent",
        logical_version="issue-999" if mode != "baseline" else "v0",
        runtime_kind="hosted_code",
        framework="synthetic_framework",
        model_contract={"id": "synthetic-model", "version": "1"},
        reviewed_mode=mode,
    )


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("baseline", (5, 5)),
        ("deterministic", (5, 5)),
        ("model_mediated", (7, 5)),
    ],
)
def test_reviewed_validation_matrix_is_fixed(
    mode: str,
    expected: tuple[int, int],
) -> None:
    assert validation_matrix(mode) == expected


def test_full_executable_body_changes_execution_digest() -> None:
    rules = _rules()
    changed = deepcopy(rules)
    changed["scenarios"][0]["attempts"][0]["probe_steps"][0]["request"]["body"][
        "max_output_tokens"
    ] = 201
    changed = stamp_execution_digests(
        changed,
        authority_id="issue-999",
        authority_kind="issue",
        canonical_agent="synthetic-agent",
        logical_version="issue-999",
        runtime_kind="hosted_code",
        framework="synthetic_framework",
        model_contract={"id": "synthetic-model", "version": "1"},
    )
    assert changed["execution_digest"] != rules["execution_digest"]


def test_validation_mode_is_never_inferred_from_runtime_kind() -> None:
    rules = _rules("model_mediated")
    _validate(rules, "model_mediated")
    with pytest.raises(ContractError, match="reviewed classification"):
        _validate(rules, "deterministic")


def test_threshold_downgrade_and_resampling_are_rejected() -> None:
    rules = _rules("model_mediated")
    rules["scenarios"][0]["n"] = 5
    rules["scenarios"][0]["attempts"] = rules["scenarios"][0]["attempts"][:5]
    with pytest.raises(ContractError, match="thresholds must remain 5/7"):
        _validate(rules, "model_mediated")


def test_attempts_require_setup_probe_and_unique_variation() -> None:
    no_setup = _rules()
    no_setup["scenarios"][0]["attempts"][0]["setup_steps"] = []
    with pytest.raises(ContractError, match="requires setup_steps"):
        _validate(no_setup)

    repeated = _rules()
    repeated["scenarios"][0]["attempts"][1]["probe_steps"][0]["request"]["body"] = (
        deepcopy(
            repeated["scenarios"][0]["attempts"][0]["probe_steps"][0]["request"][
                "body"
            ]
        )
    )
    with pytest.raises(ContractError, match="probe-body variations"):
        _validate(repeated)


def test_prompt_rules_reject_fixtures_and_runtime_selectors() -> None:
    rules = _rules("model_mediated")
    rules["scenarios"][0]["fixtures"] = [{"name": "synthetic_tool"}]
    with pytest.raises(ContractError, match="Prompt validation cannot"):
        validate_validation_rules(
            rules,
            authority_id="issue-999",
            authority_kind="issue",
            canonical_agent="synthetic-agent",
            logical_version="issue-999",
            runtime_kind="prompt",
            framework="foundry_prompt",
            model_contract={"id": "synthetic-model", "version": "1"},
            reviewed_mode="model_mediated",
        )

    rules = _rules()
    rules["scenarios"][0]["attempts"][0]["probe_steps"][0]["request"]["body"][
        "issue_selector"
    ] = "issue-999"
    with pytest.raises(ContractError, match="runtime injection selector"):
        _validate(rules)


def test_baseline_has_health_predicate_without_v0_control() -> None:
    rules = _rules("baseline")
    _validate(rules, "baseline")


def test_issue_019_is_model_mediated_five_of_seven() -> None:
    rules = read_json(
        ROOT
        / "agents"
        / "finance-agent"
        / "issues"
        / "issue-019"
        / "traffic.json"
    )["validation_rules"]
    scenario = rules["scenarios"][0]
    assert scenario["validation_mode"] == "model_mediated"
    assert (scenario["k"], scenario["n"]) == (5, 7)
    assert len(scenario["attempts"]) == 7


def test_issue_021_requires_failed_search_and_ordered_fabrication_proof() -> None:
    rules = read_json(
        ROOT
        / "agents"
        / "travel-agent"
        / "issues"
        / "issue-021"
        / "traffic.json"
    )["validation_rules"]
    scenario = rules["scenarios"][0]
    assert scenario["defect_predicate"]["required_surfaces"] == [
        "semantic",
        "trace",
    ]
    for attempt in scenario["attempts"]:
        assertions = attempt["probe_steps"][0]["expected"]["trace_assertions"]
        assert any(
            item == {
                "name": "failed_search_before_fabricated_answer",
                "kind": "operation_sequence",
                "operations": ["execute_tool", "chat"],
            }
            for item in assertions
        )
