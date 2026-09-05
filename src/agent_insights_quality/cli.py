"""Offline commands and a narrow, private production entry point.

Only run-staging/run-daily construct Azure ports. Email commands cannot send.
Test injection is Python-only; there is no CLI runtime-root or recipient override.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import date
import json
from pathlib import Path
import subprocess
import sys
import uuid

from .errors import QualityError
from .integration import RunIntegration, command_status
from .integration import private_path as _private_path, read_object as _read_object


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="aiq-quality")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="Validate reviewed catalogs, schemas and source offline")
    commands.add_parser("generate-docs", help="Generate reviewed catalog views; never alter traffic")
    staging = commands.add_parser("run-staging", help="Run or resume incremental staging")
    staging.add_argument("--full", action="store_true")
    staging.add_argument("--new-run", action="store_true", help="Start a new full run after the previous full run completed")
    daily = commands.add_parser("run-daily", help="Run or resume today's Daily measurement")
    daily.add_argument("--test-run", action="store_true")
    daily.add_argument("--rerun", type=int, default=0)
    status = commands.add_parser("status", help="Show safe local run status")
    status.add_argument("--profile", choices=("daily", "staging"), default="daily")
    for command in ("email-claim", "email-result"):
        child = commands.add_parser(command, help="Claim app-native send" if command == "email-claim"
                                    else "Record actual app-native send evidence")
        child.add_argument("--delivery-id", required=True)
        child.add_argument("--claim-id", required=True)
        if command == "email-result":
            child.add_argument("--outcome", choices=("accepted", "delivered", "rejected", "unknown"), required=True)
            child.add_argument("--result-file", type=Path, required=True)
            child.add_argument("--reconciliation", action="store_true")
    return root


def _recipient(runtime) -> str:
    from .email import TEAM_RECIPIENT, _address
    value = _read_object(_private_path(runtime, runtime.root / "config" / "email-recipient.json"))
    if set(value) != {"schema_version", "purpose", "recipient"} or (
        value["schema_version"] != "1.0.0" or value["purpose"] != "daily_test"
    ):
        raise QualityError("private_recipient_config_invalid")
    address = _address(value["recipient"])
    if address.casefold() == TEAM_RECIPIENT:
        raise QualityError("email_recipient_isolation")
    return address


def _official_source(root: Path) -> None:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "refs/remotes/origin/main"],
        cwd=root, capture_output=True, text=True, check=False,
    )
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"], cwd=root,
        capture_output=True, text=True, check=False,
    )
    if result.returncode or head.returncode or result.stdout.strip() != head.stdout.strip():
        raise QualityError("official_source_not_fetched_main")


def _committed_inputs(root: Path) -> None:
    # An uncommitted deployment edit must not reuse an older Git-bound version.
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--",
         "src", "agents", "catalogs", "schemas", "config", "pyproject.toml"],
        cwd=root, capture_output=True, text=True, check=False,
    )
    if result.returncode or result.stdout.strip():
        raise QualityError("source_inputs_not_committed")


@asynccontextmanager
async def production_ports(catalog, runtime, run_id, assessment_settings):
    from .bootstrap import azure_json, discover_environment
    from .providers import AcrImageBuilder, AzureRuntime, AzureSol
    from .providers.hosted import hosted_environment
    from .registry import AzureRegistryBlob, DeploymentRegistry

    environment = await discover_environment(runtime.environment)
    telemetry = await asyncio.to_thread(azure_json, [
        "resource", "show", "--ids", environment.application_insights_resource_id,
        "--api-version", "2020-02-02",
    ])
    properties = telemetry.get("properties") if isinstance(telemetry, dict) else None
    connection = properties.get("ConnectionString") if isinstance(properties, dict) else None
    if not isinstance(connection, str) or not connection:
        raise QualityError("telemetry_connection_unavailable")
    records = runtime.run(run_id)
    image_records = (records.read("images", missing_ok=True) or {"records": {}})["records"]
    def persist(value):
        image_records[value["artifact_key"]] = value
        records.save_progress("images", {"records": image_records})
    images = AcrImageBuilder(
        environment.registry_name, workspace=records.directory / "build",
        persist=persist, records=image_records,
    )
    hosted_variables = hosted_environment(environment, connection)
    records.save_completed("hosted-environment", hosted_variables)
    cloud = AzureRuntime(environment, images=images, hosted_environment=hosted_variables)
    sol = AzureSol(environment, deployment=assessment_settings.deployment_name)
    blob = AzureRegistryBlob(environment)
    registry = DeploymentRegistry(blob, runtime.outbox("registry"))
    try:
        await registry.load()
        yield cloud, sol, registry
    finally:
        await blob.close()


def _artifact_path(records, key: str) -> str:
    # Let RecordStore validate every path component, including reserved Windows names.
    return str(records._path("artifacts", key))


def _staging_plan(args, catalog, runtime, revision: str, today: date):
    from .runner import choose_staging, reconcile_staging_work
    from .selection import Selection
    from .state import StateError

    if args.new_run and not args.full:
        raise QualityError("staging_new_run_requires_full")
    reconcile_staging_work(catalog, runtime)
    key = "full" if args.full else "incremental"
    active = runtime.outbox("staging").read(key, missing_ok=True)
    if active is not None:
        if set(active) != {"run_id", "source_revision", "report_date", "full", "completed"} or (
            type(active["completed"]) is not bool or active["full"] is not args.full
        ):
            raise StateError("staging_resume_invalid")
        if args.new_run and not active["completed"]:
            raise QualityError("staging_unfinished_run_exists")
    if active and active["source_revision"] == revision and not args.new_run:
        report_date = date.fromisoformat(active["report_date"])
        records = runtime.run(active["run_id"])
        saved = records.read_completed("selection")
        selections = tuple(
            Selection(catalog.target(item["key"]), item["action"], tuple(item["reasons"]))
            for item in saved["targets"]
        )
        return active, report_date, selections
    run_id = f"staging-{today.isoformat()}-{revision[:12]}" + ("-full" if args.full else "")
    run_id += "-" + uuid.uuid4().hex[:8]
    selections = choose_staging(catalog, runtime, full=args.full)
    runtime.run(run_id).save_completed("selection", {"targets": [
        {"key": item.target.key, "action": item.action, "reasons": list(item.reasons)}
        for item in selections
    ]})
    active = {
        "run_id": run_id, "source_revision": revision, "report_date": today.isoformat(),
        "full": args.full, "completed": False,
    }
    # Publish the resume reference before any provider calls or target work.
    runtime.outbox("staging").save_progress(key, active)
    return active, today, selections


def _staging_status(runtime, records, active, result, warnings=()):
    statuses = [item["status"] for item in result["results"]]
    complete = "INCOMPLETE" not in statuses and not result["integrity_failure"]
    runtime.outbox("staging").save_progress(
        "full" if active["full"] else "incremental", {**active, "completed": complete},
    )
    return {
        "run_id": active["run_id"], "profile": "staging", "selected": len(statuses),
        "report_date": active["report_date"],
        "statuses": {status: statuses.count(status) for status in ("PASS", "FAIL", "INCOMPLETE")},
        "result_path": str(records._path("progress", "staging-result")), "warnings": sorted(warnings),
    }, 0 if complete else 2


async def _run(args, catalog, runtime, *, ports, integrations, today: date) -> tuple[dict, int]:
    from .contracts import Environment
    from .runner import Runner, planned_units, source_revision
    from .selection import select_daily
    from .settings import load_assessment_settings, load_settings

    is_daily = args.command == "run-daily"
    test_run = is_daily and args.test_run
    if is_daily and (args.rerun < 1 if test_run else args.rerun != 0):
        raise QualityError("runner_test_identity_invalid")
    if is_daily and not test_run:
        _official_source(catalog.root)
    _committed_inputs(catalog.root)
    revision = source_revision(catalog)
    if is_daily:
        run_id = f"daily-{today.isoformat()}" + (f"-test-{args.rerun}" if test_run else "")
        targets = select_daily(catalog, today, test_run=test_run)
        selections = None
    else:
        active, today, selections = _staging_plan(args, catalog, runtime, revision, today)
        run_id = active["run_id"]
        targets = tuple(item.target for item in selections)
    records = runtime.run(run_id)
    if not is_daily and active["completed"]:
        return _staging_status(runtime, records, active, records.read("staging-result"))
    frozen = records.read_completed("delivery-inputs", missing_ok=True) if is_daily else None
    recipient = _recipient(runtime) if is_daily and frozen is None else None
    if frozen is None:
        settings_path = catalog.root / "config" / "runtime.json"
        settings = load_settings(settings_path if settings_path.is_file() else None)
        assessment_path = _private_path(runtime, runtime.root / "config" / "assessment.json")
        assessment = load_assessment_settings(assessment_path if assessment_path.is_file() else None)
    reuse_run_id = None
    if test_run and records.read_completed("run", missing_ok=True) is None:
        last = runtime.outbox("trials").read(today.isoformat(), missing_ok=True)
        if last and last["run_id"] != run_id:
            reuse_run_id = last["run_id"]
    # An empty staging selection has no reason to discover credentials or create providers.
    if not is_daily and not targets:
        value = {"profile": "staging", "selected": 0, "status": "unchanged", "results": [], "integrity_failure": False}
        records.save_progress("staging-result", value)
        return _staging_status(runtime, records, active, value)
    async with integrations(
        catalog.root, runtime, run_id, allowed_units=planned_units(targets),
        report_date=today, test_run=test_run,
    ) as integration:
        if frozen is not None:
            result = integration.frozen_result()
            environment = Environment(**records.read_completed("environment"))
            published = integration.publish_report(result, environment, frozen["source_revision"])
            await integration.finish_publication()
            email = integration.prepare_delivery(
                result, environment, frozen["source_revision"], rerun=args.rerun,
                recipient=lambda: frozen["recipient"],
            )
            return _daily_status(runtime, records, run_id, result, email, published, integration.warnings)
        async with ports(catalog, runtime, run_id, assessment) as (cloud, sol, registry):
            runner = Runner(
                catalog, runtime, run_id, cloud, sol, registry, settings=settings,
                test_run=test_run, rerun=args.rerun if is_daily else 0,
                revision=revision, reuse_run_id=reuse_run_id, event_outbox=integration.queue_event,
            )
            try:
                integration.attach_logger(runner.logger)
                runner.initialize(targets, today, kind=runtime.environment)
                records.save_completed("assessment-settings", {
                    "deployment_name": assessment.deployment_name, "model": assessment.model,
                    "model_version": assessment.model_version, "credential": assessment.credential,
                })
                if is_daily:
                    result = await runner.run_daily(targets)
                    published = integration.publish_report(result, cloud.environment, revision)
                    await integration.finish_publication()
                    email = integration.prepare_delivery(
                        result, cloud.environment, revision, rerun=args.rerun, recipient=lambda: recipient,
                    )
                    if test_run:
                        runtime.outbox("trials").save_progress(today.isoformat(), {"run_id": run_id})
                    return _daily_status(runtime, records, run_id, result, email, published, integration.warnings)
                result = await runner.run_staging(selections)
                await integration.finish_publication()
                return _staging_status(runtime, records, active, result, integration.warnings)
            finally:
                runner.logger.close()


def _daily_status(runtime, records, run_id, result, email, published, warnings):
    pointer = records.read("quality-result")
    return {
        "run_id": run_id, "profile": "daily", "status": result.status.value,
        "score": result.score, "counts": result.counts.to_dict(),
        "coverage": result.coverage.to_dict(), "result_path": _artifact_path(records, pointer["artifact"]),
        "delivery_id": email.request.delivery_id,
        "email_record_path": str(runtime.outbox("email")._path("progress", run_id)),
        "email_status": email.status, "warnings": sorted(warnings), **published,
    }, 0 if result.team_report_eligible else 2


def _status(runtime) -> dict:
    runs = []
    directory = runtime.directory / "runs"
    for path in sorted(directory.iterdir()) if directory.exists() else ():
        if not path.is_dir():
            continue
        records = runtime.run(path.name)
        metadata = records.read_completed("run", missing_ok=True)
        if metadata is None:
            status = records.read("command-status", missing_ok=True)
            if status and status["status"] == "blocked":
                runs.append({"run_id": path.name, "status": status["status"], "code": status["code"]})
            continue
        pointer = records.read("quality-result", missing_ok=True)
        if pointer:
            value = records.read_artifact(pointer["artifact"])
            runs.append({
                "run_id": path.name, "status": value["status"],
                "result_path": _artifact_path(records, pointer["artifact"]),
            })
        else:
            staged = records.read("staging-result", missing_ok=True)
            runs.append({"run_id": path.name, "status": "recorded" if staged else "unfinished"})
    return {"profile": runtime.environment, "runs": runs}


def _catalog(root):
    from .catalogs import load_catalog
    from jsonschema.exceptions import ValidationError
    import yaml
    try:
        return load_catalog(root)
    except (OSError, ValueError, ValidationError, yaml.YAMLError) as error:
        raise QualityError("catalog_input_invalid") from error


def main(argv=None, *, root: Path | None = None, runtime_factory=None, ports=None, integrations=None, today=None) -> int:
    args = parser().parse_args(argv)
    from .catalogs import validate_catalog
    from .email import claim_email, record_email_outcome
    from .state import RuntimeStore
    root = Path.cwd() if root is None else root
    runtime_factory = RuntimeStore if runtime_factory is None else runtime_factory
    ports = production_ports if ports is None else ports
    integrations = RunIntegration if integrations is None else integrations
    try:
        if args.command in {"validate", "generate-docs"}:
            catalog = _catalog(root)
            if args.command == "generate-docs":
                from .catalog_docs import generate_catalog_views
                print(json.dumps({"generated_paths": list(generate_catalog_views(catalog))}))
            else:
                validate_catalog(catalog)
                print(json.dumps({"status": "validated", "targets": len(catalog.targets)}))
            return 0
        profile = "staging" if args.command == "run-staging" else getattr(args, "profile", "daily")
        runtime = runtime_factory(profile)
        if args.command == "status":
            print(json.dumps(_status(runtime)))
            return 0
        with runtime.ownership():
            if args.command.startswith("run-"):
                with command_status(runtime, args.command):
                    value, code = asyncio.run(_run(
                        args, _catalog(root), runtime, ports=ports, integrations=integrations,
                        today=today or date.today(),
                    ))
            elif args.command == "email-claim":
                outbox = runtime.outbox("email")
                request = claim_email(outbox, args.delivery_id, claim_id=args.claim_id)
                artifact = f"claims/{args.delivery_id}/{args.claim_id}"
                outbox.save_artifact(artifact, request.to_private_dict())
                value, code = {
                    "delivery_id": args.delivery_id, "status": "claimed",
                    "request_path": _artifact_path(outbox, artifact),
                }, 0
            else:
                path = _private_path(runtime, args.result_file)
                record = record_email_outcome(
                    runtime.outbox("email"), args.delivery_id, claim_id=args.claim_id,
                    outcome=args.outcome, provider_result=_read_object(path),
                    reconciliation=args.reconciliation,
                )
                value, code = {
                    "delivery_id": args.delivery_id, "status": record.status,
                    "inbox_delivery_confirmed": record.inbox_delivery_confirmed,
                }, 0
            print(json.dumps(value))
            return code
    except QualityError as error:
        print(json.dumps({
            "status": "blocked", "code": error.code,
            "request_accepted": error.request_accepted, "retryable": error.retryable,
        }), file=sys.stderr)
        return 2
    except OSError:
        print(json.dumps({"status": "blocked", "code": "command_io_failed"}), file=sys.stderr)
        return 2


def entrypoint() -> int:
    """Safe terminal boundary; unexpected bugs remain failing, not soft unknowns."""
    try:
        return main()
    except Exception:
        print(json.dumps({"status": "failed", "code": "unexpected_failure"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(entrypoint())
