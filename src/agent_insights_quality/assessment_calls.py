"""Daily's two durable model slots: initial, then review OR root correction.

Provider retries of definitively rejected requests remain provider-owned. A
submitted slot without a durable response is never automatically submitted again.
"""

from collections.abc import Callable, Mapping
from copy import deepcopy
from hashlib import sha256
import json

from .assessment_partition import expand_payload
from .contracts import SolPort
from .errors import QualityError
from .state import RecordStore, StateError

DAILY_CALL_CONTRACT = "daily-assessment-two-slots-v1"


def _digest(value) -> str:
    return sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=True, allow_nan=False,
    ).encode("utf-8")).hexdigest()


class DailyAssessmentCalls:
    def __init__(
        self, sol: SolPort, records: RecordStore, key: str, binding: Mapping,
        save: Callable[[RecordStore, str, str, Mapping], None],
    ) -> None:
        self.sol, self.records, self.key, self.save = sol, records, key, save
        self.binding = {**deepcopy(dict(binding)), "contract": DAILY_CALL_CONTRACT}

    async def complete_json(self, *, instructions, payload, schema):
        decoded = expand_payload(payload)
        modes = [name for name in ("review", "correction") if name in decoded]
        if len(modes) > 1:
            raise StateError("assessment_call_phase_invalid")
        mode = modes[0] if modes else "initial"
        slot = "initial" if mode == "initial" else "second"
        key = f"{self.key}/{slot}"
        self.save(self.records, "artifact", self.key + "/binding", self.binding)
        if slot == "second":
            first = self.records.read_artifact(self.key + "/initial/request")
            initial = self.records.read_artifact(self.key + "/initial/output")
            if (
                first["mode"] != "initial"
                or initial["request_hash"] != _digest(first)
                or {name: value for name, value in decoded.items() if name != mode}
                != expand_payload(first["payload"])
                or decoded[mode].get("initial") != initial["output"]
            ):
                raise StateError("assessment_call_input_mismatch")
        request = {
            "mode": mode, "instructions": instructions, "prompt_hash": _digest(instructions),
            "payload": deepcopy(payload), "schema": deepcopy(schema),
        }
        # Fixed slot keys reject changed inputs/modes rather than allocate new votes.
        self.save(self.records, "artifact", key + "/request", request)
        request_hash = _digest(request)
        output = self.records.read_artifact(key + "/output", missing_ok=True)
        failure = self.records.read_artifact(key + "/failure", missing_ok=True)
        phase = self.records.read(key, missing_ok=True)
        if output is not None:
            if failure is not None or output.get("request_hash") != request_hash:
                raise StateError("assessment_call_checkpoint_invalid")
            # The response is truth even if the process stopped before its phase save.
            self.save(self.records, "completed", key, {
                "status": "completed", "mode": mode, "request_hash": request_hash,
            })
            return deepcopy(output["output"])
        if failure is not None:
            if failure.get("request_hash") != request_hash:
                raise StateError("assessment_call_checkpoint_invalid")
            error = QualityError(
                failure["code"], request_accepted=failure["request_accepted"],
                status=failure["status"],
            )
            error.private_detail = failure.get("detail")
            error.response = failure.get("response")
            raise error
        if phase is not None:
            if phase != {"status": "submitting", "mode": mode, "request_hash": request_hash}:
                raise StateError("assessment_call_checkpoint_invalid")
            raise QualityError("assessment_call_outcome_unresolved")
        self.save(self.records, "progress", key, {
            "status": "submitting", "mode": mode, "request_hash": request_hash,
        })
        try:
            result = await self.sol.complete_json(
                instructions=instructions, payload=deepcopy(payload), schema=deepcopy(schema),
            )
        except StateError:
            raise
        except QualityError as error:
            self.save(self.records, "artifact", key + "/failure", {
                "request_hash": request_hash, "code": error.code,
                "request_accepted": error.request_accepted, "status": error.status,
                "detail": getattr(error, "private_detail", None),
                "response": getattr(error, "response", None),
            })
            raise
        self.save(self.records, "artifact", key + "/output", {
            "request_hash": request_hash, "output": result,
        })
        self.save(self.records, "completed", key, {
            "status": "completed", "mode": mode, "request_hash": request_hash,
        })
        return deepcopy(result)
