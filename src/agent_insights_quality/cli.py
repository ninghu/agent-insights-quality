"""Offline commands and a narrow, private production entry point.

Run commands construct qualification ports; private-report-flush opens only storage.
Email commands cannot send.
Test injection is Python-only; there is no CLI runtime-root override.
Unified mode/destination inputs are validated and frozen before provider work.
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
from .delivery_recipient import (
    configured_private_recipient, freeze_private_recipient, validate_test_recipient_input,
)
from .integration import RunIntegration, command_status
from .integration import private_path as _private_path, read_object as _read_object
from .performance import RunMetrics, begin_metrics, current_metrics, metric_session, scope


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="aiq-quality")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="Validate reviewed catalogs, schemas and source offline")
    commands.add_parser("generate-docs", help="Generate reviewed catalog views; never alter traffic")
    staging = commands.add_parser("run-staging", help="Run or resume incremental staging")
    staging.add_argument("--full", action="store_true")
    staging.add_argument("--new-run", action="store_true", help="Start a new full run after the previous full run completed")
    daily = commands.add_parser("run-daily", help="Run or resume today's Daily measurement")
    daily.add_argument("--report-mode", choices=("test", "official"),
                       help="Unified launch: automatic private TEST identity or official date singleton")
    daily.add_argument("--to-address", metavar="TO_ADDRESS",
                       help="One literal mailbox, required with --report-mode; frozen before traffic")
    daily.add_argument("--test-run", action="store_true", default=None,
                       help="Legacy private mode; requires a positive --rerun")
    daily.add_argument("--rerun", type=int, default=None)
    daily.add_argument(
        "--test-to", metavar="TEST_TO_ADDRESS",
        help="One human-provided private TEST recipient; requires --test-run and positive --rerun. "
             "Frozen before traffic; never an official recipient override",
    )
    daily.add_argument(
        "--fresh-traffic", action="store_true", default=None,
        help="Start a NEW private --test-run --rerun identity without reusing prior traffic; "
             "resume keeps the frozen intent even if this flag is omitted",
    )
    status = commands.add_parser("status", help="Show safe local run status")
    status.add_argument("--profile", choices=("daily", "staging"), default="daily")
    publication = commands.add_parser(
        "private-report-flush", help="Reconcile/retry one frozen private report; never remeasure or send",
    )
    publication.add_argument("--delivery-id", required=True)
    publication.add_argument(
        "--read-only", action="store_true",
        help="Read/reconcile remote blobs and save local receipts, without remote writes",
    )
    access = commands.add_parser(
        "private-report-refresh-access", help="Create a private access revision/preview only; never send",
    )
    access.add_argument("--delivery-id", required=True)
    access.add_argument("--access-revision", required=True)
    preview = commands.add_parser("email-preview", help="Export private local HTML/EML; never claim or send")
    preview.add_argument("--delivery-id", required=True)
    preview.add_argument(
        "--restyle", action="store_true",
        help="Render current presentation from this delivery's frozen result; no remeasurement",
    )
    preview.add_argument(
        "--rescore", action="store_true",
        help="With --restyle, derive a private preview under the current scoring policy; "
             "keep original results, judgments and email unchanged",
    )
    preview.add_argument(
        "--scoring-revision",
        help="Verify a published GitHub commit's QUALITY_BAR.md against the reviewed local file",
    )
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
    return configured_private_recipient(runtime)


def _daily_arguments(args) -> None:
    from .automation_launch import validate_input
    unified = args.report_mode is not None or args.to_address is not None
    if unified:
        if any(value is not None for value in (
            args.test_run, args.rerun, args.test_to, args.fresh_traffic,
        )):
            raise QualityError("automation_mixed_identity_flags")
        validate_input(args.report_mode, args.to_address)
        args.test_run = args.report_mode == "test"
        args.rerun = 0
        args.fresh_traffic = True if args.test_run else None
    else:
        args.test_run = bool(args.test_run)
        args.rerun = args.rerun if args.rerun is not None else 0
        if (args.rerun < 1 if args.test_run else args.rerun != 0):
            raise QualityError("runner_test_identity_invalid")
        validate_test_recipient_input(test_run=args.test_run, test_to=args.test_to)
        if args.fresh_traffic and not args.test_run:
            raise QualityError("fresh_traffic_requires_test_rerun")


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
    metrics = current_metrics()
    output_mode = "json_text" if (
        assessment_settings.model, assessment_settings.model_version
    ) == ("gpt-6-astra", "2026-09-03") else "json_schema"
    sol = AzureSol(
        environment, deployment=assessment_settings.deployment_name, output_mode=output_mode,
        **({"observer": metrics.observe_sol} if metrics is not None else {}),
    )
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


def _assessment_for_run(runtime, records):
    from .settings import AssessmentSettings, load_assessment_settings
    from .state import StateError

    frozen = records.read_completed("assessment-settings", missing_ok=True)
    if frozen is not None:
        return AssessmentSettings.from_dict(frozen)
    if records.read_completed("run", missing_ok=True) is not None:
        raise StateError("assessment_settings_missing")
    path = None
    if runtime.environment == "daily":
        override = _private_path(runtime, runtime.root / "config" / "daily-assessment.json")
        if override.is_file():
            path = override
    if path is None:
        shared = _private_path(runtime, runtime.root / "config" / "assessment.json")
        path = shared if shared.is_file() else None
    settings = load_assessment_settings(
        path, require_complete=path is not None and path.name == "daily-assessment.json",
    )
    records.save_completed("assessment-settings", settings.to_dict())
    return settings


async def _run(
    args, catalog, runtime, *, ports, integrations, today: date, staging_policy_migration=None,
) -> tuple[dict, int]:
    from .contracts import Environment
    from .runner import Runner, daily_traffic_intent, planned_units, source_revision
    from .selection import select_daily
    from .settings import load_settings

    is_daily = args.command == "run-daily"
    if staging_policy_migration is not None:
        from .staging_policy import STAGING_POLICY, StagingPolicyMigration
        if is_daily or not isinstance(staging_policy_migration, StagingPolicyMigration) or (
            staging_policy_migration.destination_policy != STAGING_POLICY
        ):
            raise QualityError("staging_policy_migration_invalid")
    test_run = is_daily and args.test_run
    unified = is_daily and getattr(args, "report_mode", None) is not None
    if is_daily and not unified and (args.rerun < 1 if test_run else args.rerun != 0):
        raise QualityError("runner_test_identity_invalid")
    if is_daily and args.fresh_traffic and not test_run:
        raise QualityError("fresh_traffic_requires_test_rerun")
    def checked_source():
        if is_daily and not test_run and not unified:
            _official_source(catalog.root)
        _committed_inputs(catalog.root)
        return source_revision(catalog)

    launch = None
    if unified:
        from .automation_launch import resolve_launch
        launch = resolve_launch(
            runtime, report_mode=args.report_mode, to_address=args.to_address,
            today=today, source=checked_source,
            validate_new_source=(lambda: _official_source(catalog.root)) if not test_run else None,
        )
        today = date.fromisoformat(launch["report_date"])
        args.rerun = launch["rerun"]
        revision = launch["source_revision"]
    else:
        revision = checked_source()
    if is_daily:
        run_id = f"daily-{today.isoformat()}" + (f"-test-{args.rerun}" if test_run else "")
        from .automation_launch import read_launch
        retained_launch = read_launch(runtime, run_id)
        if retained_launch is not None and retained_launch["source_revision"] != revision:
            raise QualityError("automation_resume_source_changed")
        targets = select_daily(catalog, today, test_run=test_run)
        selections = None
    else:
        active, today, selections = _staging_plan(args, catalog, runtime, revision, today)
        run_id = active["run_id"]
        targets = tuple(item.target for item in selections)
    records = runtime.run(run_id)
    private_recipient = freeze_private_recipient(
        runtime, run_id, test_run=test_run,
        test_to=launch["to_address"] if launch and test_run else getattr(args, "test_to", None),
    ) if is_daily else None
    if is_daily and (
        records.read_completed("delivery-inputs", missing_ok=True) is None
        and runtime.outbox("email").read(run_id, missing_ok=True) is not None
    ):
        raise QualityError("delivery_inputs_missing")
    traffic_intent = None
    if is_daily:
        reuse_run_id = None
        if test_run and not args.fresh_traffic and (
            records.read_completed("run", missing_ok=True) is None
            and records.read_completed("traffic-intent", missing_ok=True) is None
        ):
            last = runtime.outbox("trials").read(today.isoformat(), missing_ok=True)
            if last and last["run_id"] != run_id:
                reuse_run_id = last["run_id"]
        traffic_intent = daily_traffic_intent(
            records, test_run=test_run, rerun=args.rerun, revision=revision,
            fresh_traffic=args.fresh_traffic, reuse_run_id=reuse_run_id,
        )
    metrics = begin_metrics(records)
    if not is_daily and active["completed"]:
        settings_path = catalog.root / "config" / "runtime.json"
        requested = load_settings(settings_path if settings_path.is_file() else None)
        if requested.staging_travel_session_lookahead:
            policy = records.read_completed("staging-session-lookahead", missing_ok=True)
            if policy != {"travel_sessions_ahead": 1} or type(policy.get("travel_sessions_ahead")) is not int:
                raise QualityError("staging_session_trial_frozen_off")
        if metrics:
            metrics.reuse("run", "staging")
        return _staging_status(runtime, records, active, records.read("staging-result"))
    frozen = records.read_completed("delivery-inputs", missing_ok=True) if is_daily else None
    assessment = _assessment_for_run(runtime, records)
    if frozen is None:
        settings_path = catalog.root / "config" / "runtime.json"
        settings = load_settings(settings_path if settings_path.is_file() else None)
    # An empty staging selection has no reason to discover credentials or create providers.
    if not is_daily and not targets:
        if settings.staging_travel_session_lookahead:
            raise QualityError("staging_session_trial_requires_fresh_travel")
        value = {"profile": "staging", "selected": 0, "status": "unchanged", "results": [], "integrity_failure": False}
        records.save_progress("staging-result", value)
        return _staging_status(runtime, records, active, value)
    async with integrations(
        catalog.root, runtime, run_id, allowed_units=planned_units(targets),
        report_date=today, test_run=test_run,
    ) as integration:
        if frozen is not None:
            if metrics:
                metrics.reuse("run", "daily")
            result = integration.frozen_result()
            environment = Environment(**records.read_completed("environment"))
            with scope(metrics, "stage", "delivery_report"):
                published = integration.publish_report(result, environment, frozen["source_revision"])
                await integration.finish_publication()
                if metrics and metrics.health_warnings:
                    integration.warn("logging_failed")
                email = integration.prepare_delivery(
                    result, environment, frozen["source_revision"], rerun=args.rerun,
                    recipient=lambda: private_recipient, assessment_settings=assessment,
                )
            return _daily_status(runtime, records, run_id, result, email, published, integration.warnings)
        async with ports(catalog, runtime, run_id, assessment) as (cloud, sol, registry):
            runner = Runner(
                catalog, runtime, run_id, cloud, sol, registry, settings=settings,
                test_run=test_run, rerun=args.rerun if is_daily else 0,
                revision=revision,
                reuse_run_id=traffic_intent["reuse_run_id"] if traffic_intent else None,
                fresh_traffic=traffic_intent["fresh_traffic"] if traffic_intent else None,
                event_outbox=integration.queue_event,
                staging_policy_migration=staging_policy_migration,
                metrics=metrics, assessment_settings=assessment,
            )
            try:
                integration.attach_logger(runner.logger)
                runner.initialize(targets, today, kind=runtime.environment)
                if is_daily:
                    result = await runner.run_daily(targets)
                    with scope(metrics, "stage", "delivery_report"):
                        published = integration.publish_report(result, cloud.environment, revision)
                        await integration.finish_publication()
                        if metrics and metrics.health_warnings:
                            integration.warn("logging_failed")
                        email = integration.prepare_delivery(
                            result, cloud.environment, revision, rerun=args.rerun,
                            recipient=lambda: private_recipient,
                            assessment_settings=assessment,
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
    frozen = records.read_completed("delivery-inputs", missing_ok=True)
    presentation = frozen.get("presentation", {}) if frozen else {}
    return {
        "run_id": run_id, "profile": "daily", "status": result.status.value,
        "score": result.score, "counts": result.counts.to_dict(),
        "coverage": result.coverage.to_dict(), "result_path": _artifact_path(records, pointer["artifact"]),
        "delivery_id": email.request.delivery_id,
        "email_record_path": str(runtime.outbox("email")._path("progress", run_id)),
        "email_status": email.status, "warnings": sorted(warnings), **published,
        **({
            "private_report_markdown_path": str(
                records._path("artifacts", "presentation/report").with_suffix(".md"),
            ),
            "presentation_blockers": presentation["blockers"],
        } if presentation else {}),
    }, 0 if result.team_report_eligible else 2


def _unavailable_report_state(error):
    return {
        "status": "unavailable",
        "code": error.code if isinstance(error, QualityError) else "private_report_unavailable",
        "human_validation_available": False,
    }


def _private_report_status(runtime, run_id, *, access_descriptor=None):
    from .private_publication import PrivateReportOutbox
    from .report_access import VerifiedReportAccess, read_report_access

    try:
        outbox = PrivateReportOutbox(runtime, run_id)
        request = outbox.request()
        publication = outbox.status(request)
    except (QualityError, OSError) as error:
        return {
            **_unavailable_report_state(error),
            "access": _unavailable_report_state(error),
        }
    try:
        access = (
            read_report_access(runtime, access_descriptor, delivery_id=run_id)
            if access_descriptor is not None else VerifiedReportAccess(
                runtime, run_id + "/" + request["presentation_id"] + "/access/initial",
            )
        )
        publication["access"] = access.status()
    except (QualityError, OSError) as error:
        publication["access"] = _unavailable_report_state(error)
    return publication


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
        performance = records.read("performance/latest", missing_ok=True)
        if performance:
            runs[-1]["performance_path"] = _artifact_path(records, performance["artifact"])
        if runtime.environment == "daily":
            from .email import read_email
            email = None
            try:
                if runtime.outbox("email").read(path.name, missing_ok=True) is not None:
                    email = read_email(runtime.outbox("email"), path.name)
                    runs[-1].update(
                        email_status=email.status, inbox_delivery_confirmed=email.inbox_delivery_confirmed,
                    )
            except (QualityError, OSError) as error:
                runs[-1]["email_status"] = "unavailable"
                runs[-1]["email_status_code"] = (
                    error.code if isinstance(error, QualityError) else "email_record_unavailable"
                )
            runs[-1]["private_report"] = _private_report_status(
                runtime, path.name,
                access_descriptor=email.request.report_access if email else None,
            )
    return {"profile": runtime.environment, "runs": runs}


def _catalog(root):
    from .catalogs import load_catalog
    from jsonschema.exceptions import ValidationError
    import yaml
    try:
        return load_catalog(root)
    except (OSError, ValueError, ValidationError, yaml.YAMLError) as error:
        raise QualityError("catalog_input_invalid") from error


def main(
    argv=None, *, root: Path | None = None, runtime_factory=None, ports=None, integrations=None,
    today=None, staging_policy_migration=None, metrics_factory=RunMetrics,
) -> int:
    args = parser().parse_args(argv)
    from .catalogs import validate_catalog
    from .email import claim_email, record_email_outcome
    from .state import RuntimeStore
    root = Path.cwd() if root is None else root
    runtime_factory = RuntimeStore if runtime_factory is None else runtime_factory
    ports = production_ports if ports is None else ports
    integrations = RunIntegration if integrations is None else integrations
    try:
        if args.command == "run-daily":
            _daily_arguments(args)
        if staging_policy_migration is not None and args.command != "run-staging":
            raise QualityError("staging_policy_migration_invalid")
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
                    with metric_session(metrics_factory) as performance:
                        value, code = asyncio.run(_run(
                            args, _catalog(root), runtime, ports=ports, integrations=integrations,
                            today=today or date.today(),
                            staging_policy_migration=staging_policy_migration,
                        ))
                    if performance[0] is not None:
                        if performance[0].artifact_path:
                            value["performance_path"] = performance[0].artifact_path
                        if performance[0].health_warnings:
                            value["warnings"] = sorted({*value.get("warnings", []), "logging_failed"})
            elif args.command == "private-report-refresh-access":
                from .report_access import refresh_report_access
                with command_status(runtime, args.command):
                    status = refresh_report_access(
                        runtime, args.delivery_id, revision=args.access_revision,
                    )
                    value, code = {
                        "delivery_id": args.delivery_id, "report_access": status,
                    }, 0 if status["status"] == "ready" else 2
            elif args.command == "private-report-flush":
                from .private_publication import flush_private_report
                with command_status(runtime, args.command):
                    status = flush_private_report(
                        runtime, args.delivery_id, read_only=args.read_only,
                    )
                    value, code = {
                        "delivery_id": args.delivery_id, "private_report": status,
                    }, 0 if status["status"] == "delivered" else 2
            elif args.command == "email-preview":
                from .email_preview import export_email_preview
                preview = export_email_preview(
                    runtime, args.delivery_id, root=root, restyle=args.restyle,
                    scoring_revision=args.scoring_revision, rescore=args.rescore,
                )
                value, code = preview.to_dict(), 0
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
