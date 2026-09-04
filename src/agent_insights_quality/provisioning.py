from __future__ import annotations

import io
import json
import os
import time
import copy
import functools
import http.server
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Mapping

from agent_insights_quality.catalogs import (
    agent_model_contract,
    catalog_hashes,
    load_catalogs,
    source_integrity_digest,
)
from agent_insights_quality.azure_cli import azure_cli
from agent_insights_quality.live import (
    _azure_cli_token,
    _normalize_fixture,
    _semantic_assertion_names,
    _trace_assertion_names,
)
from agent_insights_quality.progress import ProgressReporter
from agent_insights_quality.profiles import RuntimeProfile
from agent_insights_quality.registry import (
    DEPLOYMENT_REGISTRY_SCHEMA_VERSION,
    load_registry,
    publish_registry,
)
from agent_insights_quality.reporting import (
    _baseline_runtime_evidence_complete,
    _runtime_evidence_complete,
    validate_staging_report,
)
from agent_insights_quality.run_manifest import validate_manifest
from agent_insights_quality.util import (
    ROOT,
    ContractError,
    atomic_json,
    content_hash,
    file_hash,
    read_json,
    runtime_root,
)
from agent_insights_quality.validation_rules import (
    staging_authority_requests,
    issue_observation_context,
)
from jsonschema import Draft202012Validator


def _is_package_file(path: Path) -> bool:
    return (
        path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix.casefold() not in {".pyc", ".pyo"}
    )

_AGENT_CREATE_PROPAGATION_SECONDS = 10 * 60
_AGENT_CREATE_MAX_BACKOFF_SECONDS = 30


class RemoteHttpError(ContractError):
    def __init__(self, status: int, code: str, message: str, route: str) -> None:
        super().__init__(
            f"Foundry {route} failed with HTTP {status}"
            + (f" ({code}: {message})" if code else "")
        )
        self.status = status
        self.code = code
        self.route = route

    @property
    def transient(self) -> bool:
        return self.status in {408, 429} or 500 <= self.status <= 599


def provision_profile(
    *,
    profile: RuntimeProfile,
    agents: dict[str, Any],
    issues: dict[str, Any],
    token_provider: Callable[[str], str] = _azure_cli_token,
    approved_digests: dict[str, str] | None = None,
) -> dict[str, Any]:
    reporter = ProgressReporter("aiq-provision")
    progress = reporter.emit

    progress(f"{profile.name}: verifying Test Agent model")
    model_contract = agent_model_contract(agents)
    profile.assert_test_agent_model(model_contract)
    client = FoundryProvisioner(
        profile,
        token_provider=token_provider,
        progress=reporter,
    )
    progress(f"{profile.name}: waiting for Foundry Project data plane")
    client.wait_project()
    test_region = profile.resolve_test_region()
    progress(f"{profile.name}: checking reusable registry")
    reusable = _reusable_registry(
        client,
        profile,
        agents,
        issues,
        approved_digests,
        test_region,
    )
    if reusable is not None:
        progress(f"{profile.name}: existing 41-version registry is reusable")
        publish_registry(profile)
        progress(f"{profile.name}: canonical registry published")
        return reusable
    issue_by_id = {item["id"]: item for item in issues["issues"]}
    support_agent = next(
        item for item in agents["agents"] if item["name"] == "support-ticket-agent"
    )
    progress(f"{profile.name}: resolving Support image artifacts")
    support_images = _build_support_images(
        profile,
        support_agent,
        progress=reporter,
    )
    registry_agents: dict[str, Any] = {}
    for agent in agents["agents"]:
        progress(f"{profile.name}/{agent['name']}: reconciling versions")
        versions: dict[str, Any] = {}
        logical_versions = ["v0", *agent["issue_ids"]]
        for logical_version in logical_versions:
            artifact = build_artifact(
                agent,
                issue_by_id.get(logical_version),
                support_images=support_images,
            )
            key = f"{agent['name']}/{logical_version}"
            if (
                approved_digests is not None
                and approved_digests.get(key) != artifact["content_digest"]
            ):
                raise ContractError(
                    f"{key} does not match the human-reviewed staging artifact"
                )
            version = client.ensure_version(
                agent=agent,
                logical_version=logical_version,
                artifact=artifact,
            )
            versions[logical_version] = {
                "foundry_version": version,
                "content_digest": artifact["content_digest"],
            }
            progress(
                f"{profile.name}/{agent['name']}/{logical_version}: active"
            )
        progress(f"{profile.name}/{agent['name']}: reconciling monitor")
        monitor_id = client.ensure_monitor(agent["name"])
        registry_agents[agent["name"]] = {
            "monitor_id": monitor_id,
            "versions": versions,
        }
    if not _monitor_inventory_matches(
        client._list_monitors(),
        {
            name: value["monitor_id"]
            for name, value in registry_agents.items()
        },
    ):
        raise ContractError("Profile monitor inventory is not exact")
    registry = {
        "schema_version": DEPLOYMENT_REGISTRY_SCHEMA_VERSION,
        "profile": profile.name,
        "environment_id": profile.environment_id,
        "location": profile.location,
        "telemetry_resource_set": profile.telemetry_resource_set,
        "account_name": profile.account_name,
        "project_name": profile.project_name,
        "test_region": test_region,
        "test_agent_model": model_contract,
        "catalog_hashes": catalog_hashes(agents, issues),
        "agents": registry_agents,
    }
    atomic_json(profile.registry_path, registry)
    progress(f"{profile.name}: local registry reconciled")
    publish_registry(profile)
    progress(f"{profile.name}: canonical registry published")
    return registry


def _reusable_registry(
    client: FoundryProvisioner,
    profile: RuntimeProfile,
    agents: dict[str, Any],
    issues: dict[str, Any],
    approved_digests: dict[str, str] | None,
    test_region: str,
) -> dict[str, Any] | None:
    if not profile.registry_path.exists():
        return None
    try:
        registry = load_registry(
            profile.registry_path,
            profile=profile.name,
            catalog_hashes=catalog_hashes(agents, issues),
        )
    except ContractError:
        return None
    if registry["test_region"] != test_region:
        return None
    expected_monitors = {
        name: value["monitor_id"]
        for name, value in registry["agents"].items()
    }
    if not _monitor_inventory_matches(client._list_monitors(), expected_monitors):
        return None
    for agent in agents["agents"]:
        name = agent["name"]
        client.report_progress(
            f"{profile.name}/{name}: validating reusable versions"
        )
        hosted = agent["type"] != "prompt"
        for logical_version in ["v0", *agent["issue_ids"]]:
            entry = registry["agents"][name]["versions"][logical_version]
            key = f"{name}/{logical_version}"
            if (
                approved_digests is not None
                and approved_digests.get(key) != entry["content_digest"]
            ):
                return None
            found = client._find_version(
                name,
                logical_version,
                entry["content_digest"],
                hosted=hosted,
            )
            if str(found or "") != str(entry["foundry_version"]):
                return None
            client._wait_active(
                name,
                str(found),
                hosted=hosted,
                expected_metadata={
                    "aiq_profile": profile.name,
                    "aiq_logical_version": logical_version,
                    "aiq_content_digest": entry["content_digest"],
                },
            )
            client.report_progress(
                f"{profile.name}/{name}/{logical_version}: active"
            )
    return registry


def _monitor_inventory_matches(
    monitors: list[dict[str, Any]],
    expected: dict[str, str],
) -> bool:
    grouped: dict[str, list[str]] = {}
    for item in monitors:
        if not isinstance(item, dict):
            return False
        grouped.setdefault(
            str(item.get("agent_name") or ""),
            [],
        ).append(str(item.get("id") or ""))
    return set(grouped) == set(expected) and all(
        grouped[name] == [monitor_id]
        for name, monitor_id in expected.items()
    )


def validate_promotion_receipt(
    path: Path,
    expected_hashes: dict[str, str],
    expected_model: dict[str, str],
) -> dict[str, str]:
    value = json.loads(path.read_text(encoding="utf-8"))
    schema = json.loads(
        (ROOT / "schemas" / "promotion-receipt.schema.json").read_text(
            encoding="utf-8"
        )
    )
    errors = list(Draft202012Validator(schema).iter_errors(value))
    if errors:
        raise ContractError(f"Staging promotion receipt is invalid: {errors[0].message}")
    if value["catalog_hashes"] != expected_hashes:
        raise ContractError("Staging promotion receipt catalog hashes are stale")
    if value["test_agent_model"] != expected_model:
        raise ContractError("Staging promotion receipt Test Agent model is stale")
    if value["artifact_manifest_hash"] != expected_hashes["artifacts"]:
        raise ContractError("Staging promotion receipt artifact manifest is stale")
    current_agents, current_issues = load_catalogs()
    expected_integrity = source_integrity_digest(
        current_agents,
        current_issues,
    )
    if value["source_integrity_digest"] != expected_integrity:
        raise ContractError("Staging promotion receipt source integrity is stale")
    digests = value["version_content_digests"]
    if value["deployment_manifest_hash"] != content_hash(digests):
        raise ContractError("Staging promotion receipt deployment manifest is invalid")
    return {str(key): str(digest) for key, digest in digests.items()}


def create_promotion_receipt(
    *,
    report: dict[str, Any],
    registry: dict[str, Any],
    manifest: dict[str, Any],
    issue_catalog: dict[str, Any],
    human_reviewed: bool,
) -> dict[str, Any]:
    validate_staging_report(report, issue_catalog)
    validate_manifest(manifest)
    _validate_manifest_traffic_evidence(manifest, issue_catalog)
    if manifest["profile"] != "staging":
        raise ContractError("Daily promotion requires a staging run manifest")
    if human_reviewed is not True:
        raise ContractError("Daily promotion requires explicit human review")
    if registry.get("profile") != "staging":
        raise ContractError("Daily promotion requires the staging deployment registry")
    if report.get("catalog_hashes") != registry.get("catalog_hashes"):
        raise ContractError("Staging report and deployment registry do not match")
    if (
        manifest["catalog_hashes"] != registry["catalog_hashes"]
        or report["manifest_reference"] != manifest["manifest_hash"]
        or report.get("source_integrity") != manifest["source_integrity"]
        or manifest["source_integrity"].get("verified") is not True
    ):
        raise ContractError("Staging report, manifest, and registry are not bound")
    report_baselines = {item["agent"]: item for item in report["baseline"]}
    report_issues = {item["issue_id"]: item for item in report["issues"]}
    issue_by_id = {
        issue["id"]: issue for issue in issue_catalog["issues"]
    }
    for agent in manifest["agents"]:
        baseline_complete = _baseline_runtime_evidence_complete(
            agent,
            agent["baseline"],
        )
        report_baseline = report_baselines.get(agent["name"])
        if (
            agent["baseline"]["status"] not in {"passed", "not_at_bar"}
            or
            not baseline_complete
            or report_baseline is None
            or report_baseline.get("foundry_version")
            != agent["baseline"]["foundry_version"]
            or report_baseline.get("status") != agent["baseline"]["status"]
            or report_baseline.get("runtime_evidence_complete") is not True
        ):
            raise ContractError(
                "Staging baseline runtime evidence is incomplete"
            )
        for issue in agent["issues"]:
            runtime_complete = _runtime_evidence_complete(
                issue,
                traffic_path=(
                    ROOT
                    / issue_by_id[issue["issue_id"]]["implementation"]
                    / "traffic.json"
                ),
            )
            report_issue = report_issues.get(issue["issue_id"])
            if (
                issue["status"] not in {"observed", "not_at_bar"}
                or
                not runtime_complete
                or report_issue is None
                or report_issue.get("foundry_version")
                != issue["foundry_version"]
                or report_issue.get("status") != issue["status"]
                or report_issue.get("evidence_reference")
                != issue["evidence_reference"]
                or report_issue.get("runtime_evidence_complete") is not True
            ):
                raise ContractError(
                    "Staging issue runtime or activation evidence is incomplete"
                )
    current_agents, current_issues = load_catalogs()
    if (
        manifest["source_integrity"]["contract_digest"]
        != source_integrity_digest(current_agents, current_issues)
    ):
        raise ContractError("Staging source integrity evidence is stale")
    manifest_versions = {
        f"{agent['name']}/{value['logical_version']}": value["foundry_version"]
        for agent in manifest["agents"]
        for value in [agent["baseline"], *agent["issues"]]
    }
    registry_versions = {
        f"{agent_name}/{logical_version}": value["foundry_version"]
        for agent_name, agent in registry["agents"].items()
        for logical_version, value in agent["versions"].items()
    }
    if manifest_versions != registry_versions:
        raise ContractError("Staging manifest does not cover the exact registry versions")
    digests = {
        f"{agent_name}/{logical_version}": value["content_digest"]
        for agent_name, agent in registry["agents"].items()
        for logical_version, value in agent["versions"].items()
    }
    if len(digests) != 41:
        raise ContractError("Staging registry does not contain all 41 exact versions")
    return {
        "schema_version": "2.0.0",
        "profile": "staging",
        "qualified": True,
        "human_reviewed": True,
        "quality_score": report["summary"]["quality_score"],
        "quality_score_formula": report["summary"]["quality_score_formula"],
        "test_agent_model": registry["test_agent_model"],
        "catalog_hashes": registry["catalog_hashes"],
        "artifact_manifest_hash": registry["catalog_hashes"]["artifacts"],
        "version_content_digests": digests,
        "deployment_manifest_hash": content_hash(digests),
        "qualification_manifest_hash": manifest["manifest_hash"],
        "source_integrity_digest": manifest["source_integrity"][
            "contract_digest"
        ],
        "report_reference": content_hash(report),
    }


def _validate_manifest_traffic_evidence(
    manifest: dict[str, Any],
    issue_catalog: dict[str, Any],
) -> None:
    issue_by_id = {
        issue["id"]: issue for issue in issue_catalog["issues"]
    }
    for agent in manifest["agents"]:
        for issue in agent["issues"]:
            issue_id = issue["issue_id"]
            contract = issue_by_id.get(issue_id)
            if contract is None or contract["agent"] != agent["name"]:
                raise ContractError(
                    "Manifest issue traffic is not in the reviewed catalog"
                )
            traffic_path = (
                ROOT / contract["implementation"] / "traffic.json"
            )
            requests = staging_authority_requests(traffic_path)
            summaries = issue["endpoint_request_summaries"]
            if len(requests) != len(summaries):
                raise ContractError(
                    f"{issue_id} manifest traffic coverage is inconsistent"
                )
            for raw, summary in zip(requests, summaries, strict=True):
                expected = _normalize_fixture(raw)
                assertion_names = _semantic_assertion_names(
                    expected["semantic_assertions"]
                )
                observed_names = tuple(
                    result["assertion"]
                    for result in summary["assertion_results"]
                )
                trace_assertion_names = _trace_assertion_names(
                    expected["trace_assertions"]
                )
                observed_trace_names = tuple(
                    result["assertion"]
                    for result in summary["trace_assertion_results"]
                )
                if (
                    summary["activation_gate"] != expected["activation_gate"]
                    or summary["semantic_assertion_count"]
                    != len(assertion_names)
                    or observed_names != assertion_names
                    or summary["semantic_assertions_passed"]
                    != sum(
                        result["passed"]
                        for result in summary["assertion_results"]
                    )
                    or summary["trace_assertion_count"]
                    != len(trace_assertion_names)
                    or observed_trace_names != trace_assertion_names
                    or summary["trace_assertions_passed"]
                    != sum(
                        result["passed"]
                        for result in summary["trace_assertion_results"]
                    )
                ):
                    raise ContractError(
                        f"{issue_id} manifest assertion or activation evidence "
                        "does not match authoritative traffic"
                    )
            context = issue_observation_context(traffic_path)
            observations = [
                summary for summary in summaries if summary["activation_gate"]
            ]
            required_surfaces = set(context["required_surfaces"])
            observation_count = sum(
                (
                    "semantic" not in required_surfaces
                    or (
                        summary["semantic_assertion_count"] > 0
                        and summary["semantic_assertions_passed"]
                        == summary["semantic_assertion_count"]
                    )
                )
                and (
                    "trace" not in required_surfaces
                    or (
                        summary["trace_assertion_count"] > 0
                        and summary["trace_assertions_passed"]
                        == summary["trace_assertion_count"]
                    )
                )
                for summary in observations
            )
            if (
                len(observations) != int(context["n"])
                or observation_count < int(context["k"])
            ):
                raise ContractError(
                    f"{issue_id} manifest evidence does not meet its reviewed "
                    "observation threshold"
                )


def build_artifact(
    agent: dict[str, Any],
    issue: dict[str, Any] | None,
    *,
    support_images: Mapping[str, str] | None = None,
    hosted_environment_variables: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    logical_version = issue["id"] if issue else "v0"
    root = ROOT / (
        issue["implementation"] if issue else agent["baseline_path"]
    )
    if agent["type"] == "prompt":
        definition_path = root / "definition.json"
        if not definition_path.is_file():
            raise ContractError(f"{logical_version} prompt definition is missing")
        asset = json.loads(definition_path.read_text(encoding="utf-8"))
        definition = asset.get("definition")
        if not isinstance(definition, dict) or definition.get("kind") != "prompt":
            raise ContractError(f"{logical_version} Prompt asset is invalid")
        value = {
            "kind": "prompt",
            "definition": definition,
            "content_digest": content_hash(asset),
        }
        return value
    if agent["type"] == "hosted_code":
        baseline_root = ROOT / agent["baseline_path"]
        source_root = baseline_root / "source"
        if not source_root.is_dir():
            raise ContractError(f"{agent['name']} source folder is missing")
        extra = root / "implementation.yaml" if issue else None
        source_override = root / "source" if issue else None
        archive = deterministic_zip(
            baseline_root,
            extra=extra,
            source_override=source_override,
            include=("source", "requirements.txt", "host.yaml"),
        )
        host = _read_yaml(baseline_root / "host.yaml")
        definition = _hosted_definition(host, profile_endpoint=None)
        definition["environment_variables"].update(
            hosted_environment_variables or {}
        )
        return {
            "kind": "hosted_code",
            "definition": definition,
            "archive": archive,
            "content_digest": content_hash(
                {
                    "definition": definition,
                    "archive_sha256": _sha256(archive),
                }
            ),
        }
    image = str((support_images or {}).get(logical_version) or "").strip()
    if "@sha256:" not in image:
        raise ContractError(f"Digest-pinned Support image for {logical_version} is required")
    container = _read_yaml(ROOT / agent["baseline_path"] / "container.yaml")
    definition = _container_definition(container)
    definition["environment_variables"].update(
        hosted_environment_variables or {}
    )
    definition["container_configuration"]["image"] = image
    return {
        "kind": "hosted_custom_container",
        "definition": definition,
        "content_digest": content_hash(definition),
    }


def _build_support_images(
    profile: RuntimeProfile,
    agent: dict[str, Any],
    *,
    progress: ProgressReporter | None = None,
    record_resource: Callable[[dict[str, Any]], None] | None = None,
    reusable_images: Mapping[str, str] | None = None,
) -> dict[str, str]:
    reporter = progress or ProgressReporter("aiq-provision")
    report = reporter.emit
    registry = profile.container_registry_name
    if not registry:
        raise ContractError("Owned container registry could not be resolved")
    with reporter.heartbeat("support-ticket-agent: registry sign-in") as outcome:
        login = subprocess.run(
            [azure_cli(), "acr", "login", "--name", registry, "--output", "none"],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if login.returncode != 0:
            outcome.fail()
    if login.returncode != 0:
        raise ContractError("Current Azure user cannot sign in to the owned registry")
    root = ROOT / "agents" / agent["name"]
    versions = ["v0", *agent["issue_ids"]]
    reusable = dict(reusable_images or {})
    if not set(reusable).issubset(versions) or any(
        not re.fullmatch(
            rf"{re.escape(registry)}\.azurecr\.io/"
            r"agent-insights-quality-support@sha256:[0-9a-f]{64}",
            image,
        )
        for image in reusable.values()
    ):
        raise ContractError("Reusable Support image bindings are invalid")
    tags = {
        logical: _support_image_tag(root, logical)
        for logical in versions
    }
    existing = {
        logical: reusable.get(logical)
        or _existing_acr_image(registry, tag, progress=reporter)
        for logical, tag in tags.items()
    }
    if all(existing.values()):
        report(f"{profile.name}/support-ticket-agent: all images resolved exactly")
        return {logical: str(existing[logical]) for logical in versions}
    wheelhouse = _support_wheelhouse(root, reporter)
    report(f"{profile.name}/support-ticket-agent: wheelhouse ready")
    handler = functools.partial(
        _QuietHttpHandler,
        directory=wheelhouse,
    )
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        return {
            logical: str(existing[logical])
            if existing[logical]
            else _build_and_push_support_image(
                registry=registry,
                root=root,
                logical_version=logical,
                wheelhouse_port=server.server_port,
                progress=reporter,
                record_resource=record_resource,
            )
            for logical in versions
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=30)


def _support_wheelhouse(
    root: Path,
    reporter: ProgressReporter,
) -> Path:
    requirements = root / "v0" / "requirements.txt"
    requirements_digest = file_hash(requirements)
    cache_root = (
        runtime_root()
        / "test-agent-validation"
        / "cache"
        / "support-wheelhouse"
    )
    cache_root.mkdir(parents=True, exist_ok=True)
    target = cache_root / requirements_digest.removeprefix("sha256:")
    if target.exists():
        _validate_support_wheelhouse(target, requirements_digest)
        reporter.emit("support-ticket-agent: wheelhouse found in cache")
        return target
    reporter.emit("support-ticket-agent: preparing Python wheelhouse")
    temporary = Path(
        tempfile.mkdtemp(prefix="building-", dir=cache_root)
    )
    try:
        with reporter.heartbeat(
            "support-ticket-agent: wheelhouse download"
        ) as outcome:
            download = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "download",
                    "-r",
                    str(requirements),
                    "--dest",
                    str(temporary),
                    "--platform",
                    "manylinux2014_x86_64",
                    "--python-version",
                    "312",
                    "--implementation",
                    "cp",
                    "--abi",
                    "cp312",
                    "--only-binary=:all:",
                    "--pre",
                    "--quiet",
                ],
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
            if download.returncode != 0:
                outcome.fail()
        if download.returncode != 0:
            raise ContractError(
                "Support image wheelhouse preparation failed"
            )
        files = {
            path.name: file_hash(path)
            for path in sorted(temporary.iterdir())
            if path.is_file()
        }
        if not files:
            raise ContractError(
                "Support image wheelhouse is empty"
            )
        atomic_json(
            temporary / "receipt.json",
            {
                "schema_version": "1.0.0",
                "requirements_digest": requirements_digest,
                "files": files,
            },
        )
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    _validate_support_wheelhouse(target, requirements_digest)
    return target


def _validate_support_wheelhouse(
    path: Path,
    requirements_digest: str,
) -> None:
    receipt_path = path / "receipt.json"
    if not receipt_path.is_file():
        raise ContractError(
            "Support image wheelhouse cache receipt is missing"
        )
    receipt = read_json(receipt_path)
    files = receipt.get("files")
    if (
        set(receipt) != {
            "schema_version",
            "requirements_digest",
            "files",
        }
        or receipt.get("schema_version") != "1.0.0"
        or receipt.get("requirements_digest") != requirements_digest
        or not isinstance(files, dict)
        or not files
        or files
        != {
            item.name: file_hash(item)
            for item in sorted(path.iterdir())
            if item.is_file() and item.name != "receipt.json"
        }
    ):
        raise ContractError(
            "Support image wheelhouse cache does not match requirements"
        )


def _build_and_push_support_image(
    *,
    registry: str,
    root: Path,
    logical_version: str,
    wheelhouse_port: int,
    progress: ProgressReporter | None = None,
    record_resource: Callable[[dict[str, Any]], None] | None = None,
) -> str:
    reporter = progress or ProgressReporter("aiq-provision")
    report = reporter.emit
    manifest = _support_build_context_manifest(root, logical_version)
    tag = _support_image_tag(root, logical_version, manifest=manifest)
    local_tag = f"aiq-support-{tag}:local"
    remote_tag = (
        f"{registry}.azurecr.io/agent-insights-quality-support:{tag}"
    )
    existing = _existing_acr_image(registry, tag, progress=reporter)
    if existing:
        report(f"support-ticket-agent/{logical_version}: image found in cache")
        return existing
    report(f"support-ticket-agent/{logical_version}: building image")
    install_args = (
        "--no-index --trusted-host host.docker.internal "
        f"--find-links=http://host.docker.internal:{wheelhouse_port} --pre"
    )
    with tempfile.TemporaryDirectory(
        prefix="support-build-",
        dir=runtime_root(),
    ) as temporary:
        context = Path(temporary)
        _materialize_support_build_context(manifest, context)
        with reporter.heartbeat(
            f"support-ticket-agent/{logical_version}: image build"
        ) as outcome:
            build = subprocess.run(
                [
                    "docker",
                    "build",
                    "--add-host",
                    "host.docker.internal:host-gateway",
                    "--quiet",
                    "-f",
                    str(context / "v0" / "Dockerfile"),
                    "--build-arg",
                    f"PIP_INSTALL_ARGS={install_args}",
                    "-t",
                    local_tag,
                    str(context),
                ],
                capture_output=True,
                text=True,
                timeout=900,
                check=False,
            )
            if build.returncode != 0:
                outcome.fail()
    if build.returncode != 0:
        raise ContractError(f"Support image build failed for {logical_version}")
    report(f"support-ticket-agent/{logical_version}: image built; pushing")
    authority_id = (
        "support-ticket-agent/v0"
        if logical_version == "v0"
        else logical_version
    )
    tag_resource = {
        "kind": "acr_tag",
        "intent_reference": remote_tag,
        "deterministic_name": f"agent-insights-quality-support:{tag}",
        "authority_id": authority_id,
        "parent_id": None,
    }
    manifest_resource = {
        "kind": "acr_manifest",
        "intent_reference": content_hash(
            {
                "kind": "acr_manifest",
                "repository": "agent-insights-quality-support",
                "tag": tag,
            }
        ),
        "deterministic_name": "agent-insights-quality-support",
        "authority_id": authority_id,
        "parent_id": None,
    }
    if record_resource is not None:
        record_resource({**tag_resource, "state": "create_intent"})
        record_resource({**manifest_resource, "state": "create_intent"})
    try:
        subprocess.run(
            ["docker", "tag", local_tag, remote_tag],
            capture_output=True,
            timeout=60,
            check=True,
        )
        for attempt in range(3):
            with reporter.heartbeat(
                f"support-ticket-agent/{logical_version}: image push"
            ) as outcome:
                push = subprocess.run(
                    ["docker", "push", remote_tag],
                    capture_output=True,
                    text=True,
                    timeout=900,
                    check=False,
                )
                if push.returncode != 0:
                    outcome.fail()
            match = re.search(r"digest:\s*(sha256:[0-9a-f]{64})", push.stdout)
            if push.returncode == 0 and match:
                if record_resource is not None:
                    record_resource(
                        {
                            **tag_resource,
                            "state": "created",
                            "provider_id": remote_tag,
                        }
                    )
                    record_resource(
                        {
                            **manifest_resource,
                            "state": "created",
                            "provider_id": match.group(1),
                        }
                    )
                report(f"support-ticket-agent/{logical_version}: image published")
                return (
                    f"{registry}.azurecr.io/agent-insights-quality-support@"
                    f"{match.group(1)}"
                )
            recovered = _existing_acr_image(registry, tag, progress=reporter)
            if recovered:
                if record_resource is not None:
                    digest = recovered.rsplit("@", 1)[-1]
                    record_resource(
                        {
                            **tag_resource,
                            "state": "created",
                            "provider_id": remote_tag,
                        }
                    )
                    record_resource(
                        {
                            **manifest_resource,
                            "state": "created",
                            "provider_id": digest,
                        }
                    )
                report(
                    f"support-ticket-agent/{logical_version}: published image recovered"
                )
                return recovered
            if attempt < 2:
                report(
                    f"support-ticket-agent/{logical_version}: image push retry "
                    f"{attempt + 2}/3"
                )
                time.sleep(30 * (attempt + 1))
        raise ContractError(f"Support image push failed for {logical_version}")
    except (ContractError, OSError, subprocess.SubprocessError) as error:
        if record_resource is not None:
            record_resource({**tag_resource, "state": "ambiguous_create"})
            record_resource({**manifest_resource, "state": "ambiguous_create"})
        if isinstance(error, ContractError):
            raise
        raise ContractError(
            f"Support image publication failed for {logical_version}"
        ) from error
    finally:
        try:
            with reporter.heartbeat(
                f"support-ticket-agent/{logical_version}: local image cleanup"
            ) as outcome:
                cleanup = subprocess.run(
                    ["docker", "image", "rm", local_tag, remote_tag],
                    capture_output=True,
                    timeout=120,
                    check=False,
                )
                if cleanup.returncode != 0:
                    outcome.fail()
        except (subprocess.SubprocessError, OSError):
            report(
                f"support-ticket-agent/{logical_version}: "
                "local image cleanup failed; continuing"
            )


def _support_build_context_manifest(
    root: Path,
    logical_version: str,
) -> dict[str, Path]:
    baseline = root / "v0"
    implementation = (
        baseline / "implementation.yaml"
        if logical_version == "v0"
        else root / "issues" / logical_version / "implementation.yaml"
    )
    source = (
        baseline / "source"
        if logical_version == "v0"
        else root / "issues" / logical_version / "source"
    )
    if not implementation.is_file() or not source.is_dir():
        raise ContractError(
            f"Support build authority for {logical_version} is incomplete"
        )
    manifest = {
        f"v0/{path.relative_to(baseline).as_posix()}": path
        for path in sorted(baseline.rglob("*"))
        if _is_package_file(path)
        and path != baseline / "implementation.yaml"
        and (
            logical_version == "v0"
            or (baseline / "source") not in path.parents
        )
    }
    source_files = [path for path in sorted(source.rglob("*")) if _is_package_file(path)]
    if not source_files:
        raise ContractError(f"Support source authority for {logical_version} is empty")
    manifest.update(
        {
            f"v0/source/{path.relative_to(source).as_posix()}": path
            for path in source_files
        }
    )
    manifest["v0/implementation.yaml"] = implementation
    return dict(sorted(manifest.items()))


def _support_build_context_digest(
    root: Path,
    logical_version: str,
) -> str:
    return _support_build_context_manifest_digest(
        _support_build_context_manifest(root, logical_version)
    )


def _support_build_context_digest_at_commit(
    root: Path,
    logical_version: str,
    commit_sha: str,
) -> str | None:
    if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
        raise ContractError("Support image migration commit is invalid")
    try:
        relative_root = root.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError as error:
        raise ContractError("Support build authority escapes the repository") from error
    positive_paths = [f"{relative_root}/v0"]
    if logical_version != "v0":
        positive_paths.extend(
            [
                f"{relative_root}/issues/{logical_version}/source",
                f"{relative_root}/issues/{logical_version}/implementation.yaml",
            ]
        )
    listed = subprocess.run(
        [
            "git",
            "ls-tree",
            "-r",
            "--name-only",
            "-z",
            commit_sha,
            "--",
            *positive_paths,
        ],
        cwd=ROOT,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if listed.returncode != 0:
        raise ContractError("Support image migration commit could not be read")
    retained_paths = {
        item.decode("utf-8")
        for item in listed.stdout.split(b"\0")
        if item
    }
    if logical_version != "v0":
        baseline_source = f"{relative_root}/v0/source/"
        retained_paths = {
            path
            for path in retained_paths
            if not path.startswith(baseline_source)
            and path != f"{relative_root}/v0/implementation.yaml"
        }
    manifest = _support_build_context_manifest(root, logical_version)
    current_paths = {
        source.resolve().relative_to(ROOT.resolve()).as_posix()
        for source in manifest.values()
    }
    if retained_paths != current_paths:
        return None
    paths = [*positive_paths]
    if logical_version != "v0":
        paths.extend(
            [
                f":(exclude){relative_root}/v0/source",
                f":(exclude){relative_root}/v0/implementation.yaml",
            ]
        )
    compared = subprocess.run(
        ["git", "diff", "--quiet", commit_sha, "--", *paths],
        cwd=ROOT,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if compared.returncode == 1:
        return None
    if compared.returncode != 0:
        raise ContractError("Support image migration commit could not be compared")
    return _support_build_context_manifest_digest(manifest)


def _support_build_context_manifest_digest(
    manifest: Mapping[str, Path],
) -> str:
    return content_hash(
        {
            relative_path: file_hash(source)
            for relative_path, source in sorted(manifest.items())
        }
    )


def _materialize_support_build_context(
    manifest: Mapping[str, Path],
    destination: Path,
) -> None:
    for relative_path, source in sorted(manifest.items()):
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _support_image_tag(
    root: Path,
    logical_version: str,
    *,
    manifest: Mapping[str, Path] | None = None,
) -> str:
    effective = manifest or _support_build_context_manifest(root, logical_version)
    return _support_build_context_manifest_digest(effective).removeprefix("sha256:")


def _existing_acr_image(
    registry: str,
    tag: str,
    *,
    progress: ProgressReporter | None = None,
) -> str | None:
    reporter = progress or ProgressReporter("aiq-provision")
    with reporter.heartbeat("Support image cache lookup") as outcome:
        process = subprocess.run(
            [
                azure_cli(),
                "acr",
                "manifest",
                "show-metadata",
                "--registry",
                registry,
                "--name",
                f"agent-insights-quality-support:{tag}",
                "--query",
                "digest",
                "--output",
                "tsv",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if process.returncode != 0:
            outcome.fail()
    digest = process.stdout.strip()
    if process.returncode == 0 and re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        return f"{registry}.azurecr.io/agent-insights-quality-support@{digest}"
    return None


class _QuietHttpHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        del format, args


def deterministic_zip(
    source: Path,
    *,
    extra: Path | None = None,
    source_override: Path | None = None,
    include: tuple[str, ...] | None = None,
) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        candidates: list[Path] = []
        if include is None:
            candidates = list(source.rglob("*"))
        else:
            for item in include:
                path = source / item
                candidates.extend(path.rglob("*") if path.is_dir() else [path])
        for path in sorted(candidates):
            if not _is_package_file(path):
                continue
            name = path.relative_to(source).as_posix()
            data_path = path
            if source_override is not None and name.startswith("source/"):
                candidate = source_override / Path(name).relative_to("source")
                if not candidate.is_file():
                    raise ContractError(
                        f"Hosted issue source is missing {name}"
                    )
                data_path = candidate
            info = zipfile.ZipInfo(name)
            info.date_time = (1980, 1, 1, 0, 0, 0)
            info.external_attr = 0o644 << 16
            archive.writestr(info, data_path.read_bytes())
        if extra is not None:
            info = zipfile.ZipInfo("issue.yaml")
            info.date_time = (1980, 1, 1, 0, 0, 0)
            info.external_attr = 0o644 << 16
            archive.writestr(info, extra.read_bytes())
    return output.getvalue()


def _read_yaml(path: Path) -> dict[str, Any]:
    import yaml

    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ContractError(f"{path.name} is invalid")
    return value


def _hosted_definition(
    host: dict[str, Any],
    *,
    profile_endpoint: str | None,
) -> dict[str, Any]:
    del profile_endpoint
    return {
        "kind": "hosted",
        "protocol_versions": [{"protocol": "responses", "version": "1.0.0"}],
        "cpu": "1",
        "memory": "2Gi",
        "code_configuration": {
            "runtime": "python_3_13",
            "entry_point": str(host["entrypoint"]).split(),
            "dependency_resolution": "remote_build",
        },
        "environment_variables": {
            "AZURE_AI_MODEL_DEPLOYMENT_NAME": "gpt-5.4-mini",
        },
    }


def _container_definition(container: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "hosted",
        "protocol_versions": [{"protocol": "responses", "version": "1.0.0"}],
        "cpu": "1",
        "memory": "2Gi",
        "container_configuration": {"image": "${DIGEST_PINNED_IMAGE}"},
        "environment_variables": {
            "AZURE_AI_MODEL_DEPLOYMENT_NAME": "gpt-5.4-mini",
        },
    }


class FoundryProvisioner:
    def __init__(
        self,
        profile: RuntimeProfile,
        *,
        token_provider: Callable[[str], str],
        progress: ProgressReporter | None = None,
    ) -> None:
        self._profile = profile
        self._token_provider = token_provider
        self._progress = progress or ProgressReporter("aiq-provision")
        self._project_ready_at: float | None = None

    def report_progress(self, message: str) -> None:
        self._progress.emit(message)

    def wait_project(self) -> None:
        deadline = time.monotonic() + 15 * 60
        next_progress = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                response = self._request(
                    "GET",
                    "/agents?limit=1",
                    hosted=False,
                    expected={200, 403, 404, 429, 500, 502, 503, 504},
                )
            except RemoteHttpError as error:
                if not error.transient:
                    raise
                response = {"_status": error.status}
            if response["_status"] == 200:
                self._project_ready_at = time.monotonic()
                self.report_progress(
                    f"{self._profile.name}: Foundry Project data plane ready"
                )
                return
            if time.monotonic() >= next_progress:
                self.report_progress(
                    f"{self._profile.name}: still waiting for Foundry Project"
                )
                next_progress = time.monotonic() + 60
            time.sleep(10)
        raise ContractError("Foundry Project data plane was not ready within 15 minutes")

    def version_details(
        self,
        name: str,
        version: str,
        *,
        hosted: bool,
        exact_listing_confirmed_at: float | None = None,
        logical_version: str | None = None,
    ) -> dict[str, Any]:
        if not name or not version:
            raise ContractError("Foundry Agent version identity is required")
        if exact_listing_confirmed_at is not None and not logical_version:
            raise ContractError(
                "Foundry Agent version readiness identity is required"
            )
        not_found_attempt = 0
        while True:
            try:
                return self._request(
                    "GET",
                    f"/agents/{urllib.parse.quote(name, safe='')}/versions/"
                    f"{urllib.parse.quote(version, safe='')}",
                    hosted=hosted,
                )
            except RemoteHttpError as error:
                if not self._version_read_not_found_is_transient(
                    error,
                    name=name,
                    version=version,
                    exact_listing_confirmed_at=exact_listing_confirmed_at,
                ):
                    raise
                assert exact_listing_confirmed_at is not None
                elapsed = time.monotonic() - exact_listing_confirmed_at
                remaining = _AGENT_CREATE_PROPAGATION_SECONDS - elapsed
                if remaining <= 0:
                    raise
                delay = min(
                    5 * (2**min(not_found_attempt, 3)),
                    _AGENT_CREATE_MAX_BACKOFF_SECONDS,
                    remaining,
                )
                not_found_attempt += 1
                self.report_progress(
                    f"{self._profile.name}/{name}/{logical_version}: "
                    "exact version readiness propagation pending "
                    f"at {elapsed:.0f}s; retrying in {delay:.0f}s"
                )
                time.sleep(delay)

    def delete_agent(self, name: str, *, hosted: bool) -> None:
        self._request(
            "DELETE",
            f"/agents/{urllib.parse.quote(name, safe='')}",
            hosted=hosted,
            expected={200, 202, 204, 404},
        )

    def version_exists(self, name: str, version: str, *, hosted: bool) -> bool:
        response = self._request(
            "GET",
            f"/agents/{urllib.parse.quote(name, safe='')}/versions/"
            f"{urllib.parse.quote(version, safe='')}",
            hosted=hosted,
            expected={200, 404},
            include_payload=False,
        )
        return response["_status"] == 200

    def agent_exists(self, name: str, *, hosted: bool) -> bool:
        return self._agent_exists(name, hosted=hosted)

    def delete_session(self, agent_name: str, session_id: str) -> None:
        self._request(
            "DELETE",
            f"/agents/{urllib.parse.quote(agent_name, safe='')}/endpoint/"
            f"sessions/{urllib.parse.quote(session_id, safe='')}",
            hosted=True,
            expected={200, 202, 204, 404},
        )

    def session_exists(self, agent_name: str, session_id: str) -> bool:
        response = self._request(
            "GET",
            f"/agents/{urllib.parse.quote(agent_name, safe='')}/endpoint/"
            f"sessions/{urllib.parse.quote(session_id, safe='')}",
            hosted=True,
            expected={200, 404},
            include_payload=False,
        )
        return response["_status"] == 200

    def delete_response(self, response_id: str) -> None:
        self._request(
            "DELETE",
            f"/openai/v1/responses/{urllib.parse.quote(response_id, safe='')}",
            hosted=False,
            expected={200, 202, 204, 404},
        )

    def response_exists(self, response_id: str) -> bool:
        response = self._request(
            "GET",
            f"/openai/v1/responses/{urllib.parse.quote(response_id, safe='')}",
            hosted=False,
            expected={200, 404},
            include_payload=False,
        )
        return response["_status"] == 200

    def ensure_version(
        self,
        *,
        agent: dict[str, Any],
        logical_version: str,
        artifact: dict[str, Any],
    ) -> str:
        version, _ = self._ensure_version(
            agent=agent,
            logical_version=logical_version,
            artifact=artifact,
        )
        return version

    def ensure_version_for_readiness(
        self,
        *,
        agent: dict[str, Any],
        logical_version: str,
        artifact: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        return self._ensure_version(
            agent=agent,
            logical_version=logical_version,
            artifact=artifact,
        )

    def create_version_for_readiness(
        self,
        *,
        agent: dict[str, Any],
        logical_version: str,
        artifact: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        return self._ensure_version(
            agent=agent,
            logical_version=logical_version,
            artifact=artifact,
            reuse_existing=False,
        )

    def _ensure_version(
        self,
        *,
        agent: dict[str, Any],
        logical_version: str,
        artifact: dict[str, Any],
        reuse_existing: bool = True,
    ) -> tuple[str, dict[str, Any]]:
        existing = (
            self._find_version(
                agent["name"],
                logical_version,
                artifact["content_digest"],
                hosted=agent["type"] != "prompt",
            )
            if reuse_existing
            else None
        )
        if existing:
            details = self._wait_active(
                agent["name"],
                existing,
                hosted=agent["type"] != "prompt",
                expected_metadata={
                    "aiq_profile": self._profile.name,
                    "aiq_logical_version": logical_version,
                    "aiq_content_digest": artifact["content_digest"],
                },
                not_found_confirmed_at=None,
            )
            return existing, details
        create_agent = not self._agent_exists(
            agent["name"],
            hosted=agent["type"] != "prompt",
        )
        metadata = {
            "aiq_profile": self._profile.name,
            "aiq_logical_version": logical_version,
            "aiq_content_digest": artifact["content_digest"],
        }
        remote_definition = _resolve_definition(
            artifact["definition"],
            project_endpoint=self._profile.project_endpoint,
        )
        version = ""
        transient_attempt = 0
        propagation_attempt = 0
        while True:
            try:
                if artifact["kind"] == "hosted_code":
                    version = self._create_source_version(
                        agent["name"],
                        remote_definition,
                        artifact["archive"],
                        metadata,
                        create_agent=create_agent,
                    )
                else:
                    body = {
                        "definition": remote_definition,
                        "metadata": metadata,
                    }
                    if create_agent:
                        body["name"] = agent["name"]
                    path = (
                        "/agents"
                        if create_agent
                        else f"/agents/{urllib.parse.quote(agent['name'], safe='')}/versions"
                    )
                    response = self._request(
                        "POST",
                        path,
                        body=json.dumps(body).encode("utf-8"),
                        hosted=agent["type"] != "prompt",
                    )
                    version = _version_from_response(response)
                break
            except RemoteHttpError as error:
                if not reuse_existing:
                    raise
                if self._agent_create_not_found_is_transient(
                    error,
                    create_agent=create_agent,
                ):
                    assert self._project_ready_at is not None
                    elapsed = time.monotonic() - self._project_ready_at
                    remaining = (
                        _AGENT_CREATE_PROPAGATION_SECONDS - elapsed
                    )
                    if remaining <= 0:
                        raise
                    delay = min(
                        5 * (2**min(propagation_attempt, 3)),
                        _AGENT_CREATE_MAX_BACKOFF_SECONDS,
                        remaining,
                    )
                    propagation_attempt += 1
                    self.report_progress(
                        f"{self._profile.name}/{agent['name']}/"
                        f"{logical_version}: Agent-create propagation pending "
                        f"at {elapsed:.0f}s; retrying in {delay:.0f}s"
                    )
                    time.sleep(delay)
                    recovered = self._find_version(
                        agent["name"],
                        logical_version,
                        artifact["content_digest"],
                        hosted=agent["type"] != "prompt",
                    )
                    if recovered:
                        version = recovered
                        break
                    create_agent = not self._agent_exists(
                        agent["name"],
                        hosted=agent["type"] != "prompt",
                    )
                    continue
                if not error.transient or transient_attempt == 2:
                    raise
                transient_attempt += 1
                self.report_progress(
                    f"{self._profile.name}/{agent['name']}/{logical_version}: "
                    "transient create failure; recovering "
                    f"({transient_attempt + 1}/3)"
                )
                time.sleep(5)
                recovered = self._find_version(
                    agent["name"],
                    logical_version,
                    artifact["content_digest"],
                    hosted=agent["type"] != "prompt",
                )
                if recovered:
                    version = recovered
                    break
                time.sleep(60 * transient_attempt)
                create_agent = not self._agent_exists(
                    agent["name"],
                    hosted=agent["type"] != "prompt",
                )
        created_version_confirmed_at = time.monotonic()
        if reuse_existing:
            recovered = self._recover_exact_version(
                agent["name"],
                logical_version,
                artifact["content_digest"],
                hosted=agent["type"] != "prompt",
            )
            created_version_confirmed_at = (
                time.monotonic() if recovered is not None else None
            )
            if recovered:
                version = recovered
        if not version:
            raise ContractError("Foundry version creation returned no version")
        details = self._wait_active(
            agent["name"],
            version,
            hosted=agent["type"] != "prompt",
            expected_metadata=metadata,
            not_found_confirmed_at=created_version_confirmed_at,
        )
        return version, details

    def _agent_create_not_found_is_transient(
        self,
        error: RemoteHttpError,
        *,
        create_agent: bool,
    ) -> bool:
        ready_at = self._project_ready_at
        return (
            create_agent
            and ready_at is not None
            and error.status == 404
            and error.code.casefold() == "notfound"
            and error.route == "POST /agents"
            and time.monotonic() - ready_at
            < _AGENT_CREATE_PROPAGATION_SECONDS
        )

    def _recover_exact_version(
        self,
        name: str,
        logical_version: str,
        digest: str,
        *,
        hosted: bool,
    ) -> str | None:
        deadline = time.monotonic() + 15 * 60
        next_progress = time.monotonic() + 60
        while time.monotonic() < deadline:
            recovered = self._find_version(
                name,
                logical_version,
                digest,
                hosted=hosted,
            )
            if recovered:
                return recovered
            if time.monotonic() >= next_progress:
                self.report_progress(
                    f"{self._profile.name}/{name}/{logical_version}: "
                    "waiting for exact version visibility"
                )
                next_progress = time.monotonic() + 60
            time.sleep(5)
        return None

    def ensure_monitor(self, agent_name: str) -> str:
        values = self._list_monitors()
        matches = [
            item
            for item in values
            if isinstance(item, dict) and item.get("agent_name") == agent_name
        ]
        if len(matches) > 1:
            raise ContractError(f"{agent_name} has multiple monitors")
        if matches:
            return str(matches[0]["id"])
        payload = self._insights_request(
            "POST",
            "/agent_insight_monitors",
            {
                "agent_name": agent_name,
                "enabled": False,
                "run_interval_hours": 24,
                "model_deployment_name": "terra-insight-generation",
            },
        )
        monitor_id = str(payload.get("id") or "")
        if not monitor_id:
            raise ContractError("Monitor creation returned no identity")
        return monitor_id

    def _list_monitors(self) -> list[dict[str, Any]]:
        path = "/agent_insight_monitors?limit=100"
        values: list[dict[str, Any]] = []
        for _ in range(5):
            payload = self._insights_request("GET", path)
            page = payload.get("data") or payload.get("value") or payload.get("items") or []
            values.extend(item for item in page if isinstance(item, dict))
            next_link = payload.get("next_link") or payload.get("nextLink")
            if not next_link:
                return values
            parsed = urllib.parse.urlparse(str(next_link))
            path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        raise ContractError("Monitor pagination exceeded the bounded page limit")

    def _agent_exists(self, name: str, *, hosted: bool) -> bool:
        response = self._request(
            "GET",
            f"/agents/{urllib.parse.quote(name, safe='')}",
            hosted=hosted,
            expected={200, 404},
            include_payload=False,
        )
        return response["_status"] == 200

    def _find_version(
        self,
        name: str,
        logical_version: str,
        digest: str,
        *,
        hosted: bool,
    ) -> str | None:
        response = self._request(
            "GET",
            f"/agents/{urllib.parse.quote(name, safe='')}/versions?limit=100",
            hosted=hosted,
            expected={200, 404},
        )
        if response["_status"] == 404:
            return None
        candidates = response.get("data") or response.get("value") or []
        matches: list[dict[str, Any]] = []
        for item in candidates:
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata")
            if (
                isinstance(metadata, dict)
                and metadata.get("aiq_profile") == self._profile.name
                and metadata.get("aiq_logical_version") == logical_version
                and metadata.get("aiq_content_digest") == digest
            ):
                matches.append(item)
        if not matches:
            return None
        active = [
            item for item in matches if str(item.get("status") or "").lower() == "active"
        ]
        if not active:
            return None
        if len(active) != 1:
            raise ContractError(
                "Foundry Agent has ambiguous active versions for one content digest"
            )
        return str(active[0].get("version") or "")

    def _delete_owned_version(
        self,
        name: str,
        version: str,
        *,
        hosted: bool,
    ) -> None:
        if not version:
            raise ContractError("Owned duplicate version has no identity")
        self._request(
            "DELETE",
            f"/agents/{urllib.parse.quote(name, safe='')}/versions/"
            f"{urllib.parse.quote(version, safe='')}",
            hosted=hosted,
            expected={200, 202, 204, 404},
        )

    def _create_source_version(
        self,
        name: str,
        definition: dict[str, Any],
        archive: bytes,
        metadata: dict[str, str],
        *,
        create_agent: bool,
    ) -> str:
        boundary = "aiq" + _sha256(archive)[:24]
        parts = [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="metadata"\r\n',
            b"Content-Type: application/json\r\n\r\n",
            json.dumps({"definition": definition, "metadata": metadata}).encode(),
            b"\r\n",
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="code"; filename="agent.zip"\r\n',
            b"Content-Type: application/zip\r\n\r\n",
            archive,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
        path = (
            "/agents"
            if create_agent
            else f"/agents/{urllib.parse.quote(name, safe='')}/versions"
        )
        response = self._request(
            "POST",
            path,
            body=b"".join(parts),
            hosted=True,
            extra_headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "x-ms-agent-name": name,
                "x-ms-code-zip-sha256": _sha256(archive),
            },
        )
        return _version_from_response(response)

    def _wait_active(
        self,
        name: str,
        version: str,
        *,
        hosted: bool,
        expected_metadata: dict[str, str],
        not_found_confirmed_at: float | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + 30 * 60
        next_progress = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                response = self.version_details(
                    name,
                    version,
                    hosted=hosted,
                    exact_listing_confirmed_at=not_found_confirmed_at,
                    logical_version=(
                        expected_metadata["aiq_logical_version"]
                        if not_found_confirmed_at is not None
                        else None
                    ),
                )
            except RemoteHttpError as error:
                if not error.transient:
                    raise
                self.report_progress(
                    f"{self._profile.name}/{name}/"
                    f"{expected_metadata['aiq_logical_version']}: "
                    "activation check transient; retrying"
                )
                time.sleep(5)
                continue
            status = str(response.get("status") or "").lower()
            if status == "active":
                metadata = response.get("metadata")
                if not isinstance(metadata, dict) or any(
                    metadata.get(key) != value
                    for key, value in expected_metadata.items()
                ):
                    raise ContractError("Active version metadata does not match its artifact")
                return response
            if status in {"failed", "canceled", "deleted"}:
                code = ""
                if isinstance(response.get("error"), dict):
                    code = str(response["error"].get("code") or "")
                logical = expected_metadata["aiq_logical_version"]
                raise ContractError(
                    f"{name}/{logical} reached terminal state {status}"
                    + (f" ({code})" if code else "")
                )
            if time.monotonic() >= next_progress:
                logical = expected_metadata["aiq_logical_version"]
                self.report_progress(
                    f"{self._profile.name}/{name}/{logical}: "
                    f"activation status {status or 'pending'}"
                )
                next_progress = time.monotonic() + 60
            time.sleep(5)
        raise ContractError("Foundry version did not activate before the deadline")

    def _version_read_not_found_is_transient(
        self,
        error: RemoteHttpError,
        *,
        name: str,
        version: str,
        exact_listing_confirmed_at: float | None,
    ) -> bool:
        expected_route = (
            "GET /agents/"
            f"{urllib.parse.quote(name, safe='')}/versions/"
            f"{urllib.parse.quote(version, safe='')}"
        )
        return (
            exact_listing_confirmed_at is not None
            and error.status == 404
            and error.code.casefold() == "notfound"
            and error.route == expected_route
            and time.monotonic() - exact_listing_confirmed_at
            < _AGENT_CREATE_PROPAGATION_SECONDS
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        hosted: bool,
        expected: set[int] | None = None,
        include_payload: bool = True,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "Authorization": "Bearer "
            + self._token_provider("https://ai.azure.com/.default"),
        }
        if body is not None and not extra_headers:
            headers["Content-Type"] = "application/json"
        if hosted:
            headers["Foundry-Features"] = "HostedAgents=V1Preview"
        headers.update(extra_headers or {})
        attempts = 5 if method == "GET" else 1
        status = 0
        payload = b""
        for attempt in range(attempts):
            url = self._profile.project_endpoint + path
            if not path.startswith("/openai/v1/"):
                url += ("&" if "?" in path else "?") + "api-version=v1"
            request = urllib.request.Request(
                url,
                data=body,
                headers=headers,
                method=method,
            )
            try:
                with self._progress.heartbeat(f"Foundry {method} operation"):
                    with urllib.request.urlopen(request, timeout=300) as response:
                        status = response.status
                        payload = response.read()
            except urllib.error.HTTPError as error:
                status = error.code
                payload = error.read()
            except (TimeoutError, urllib.error.URLError):
                if attempt + 1 == attempts:
                    raise ContractError(
                        "Foundry read failed before a response was received"
                    ) from None
                time.sleep(2**attempt)
                continue
            if (
                method != "GET"
                or status not in {408, 429, 500, 502, 503, 504}
                or attempt + 1 == attempts
            ):
                break
            time.sleep(2**attempt)
        if status not in (expected or {200, 201, 202}):
            code, message = _remote_error(payload)
            safe_code = (
                code
                if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code)
                else "unavailable"
            )
            self.report_progress(
                f"Foundry {method} rejected: status={status}; code={safe_code}"
            )
            raise RemoteHttpError(
                status,
                code,
                message,
                f"{method} {path.split('?')[0]}",
            )
        value = json.loads(payload) if include_payload and payload else {}
        if not isinstance(value, dict):
            raise ContractError("Foundry returned an invalid payload")
        value["_status"] = status
        return value

    def _insights_request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "Authorization": "Bearer "
            + self._token_provider("https://ai.azure.com/.default"),
        }
        data = json.dumps(body).encode() if body is not None else None
        if data:
            headers["Content-Type"] = "application/json"
        attempts = 5 if method == "GET" else 1
        payload = b""
        for attempt in range(attempts):
            request = urllib.request.Request(
                self._profile.insights_endpoint
                + path
                + ("&" if "?" in path else "?")
                + "api-version=v1",
                data=data,
                headers=headers,
                method=method,
            )
            try:
                with self._progress.heartbeat(
                    f"Agent Insights {method} operation"
                ):
                    with urllib.request.urlopen(request, timeout=300) as response:
                        payload = response.read()
            except urllib.error.HTTPError as error:
                if (
                    method == "GET"
                    and error.code in {408, 429, 500, 502, 503, 504}
                    and attempt + 1 < attempts
                ):
                    time.sleep(2**attempt)
                    continue
                raise ContractError(
                    f"Agent Insights operation failed with HTTP {error.code}"
                ) from None
            except (TimeoutError, urllib.error.URLError):
                if method == "GET" and attempt + 1 < attempts:
                    time.sleep(2**attempt)
                    continue
                raise ContractError(
                    "Agent Insights operation failed before a response"
                ) from None
            break
        value = json.loads(payload) if payload else {}
        if not isinstance(value, dict):
            raise ContractError("Agent Insights returned an invalid payload")
        return value


def _sha256(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()


def _version_from_response(value: dict[str, Any]) -> str:
    direct = str(value.get("version") or "")
    versions = value.get("versions")
    latest = versions.get("latest") if isinstance(versions, dict) else None
    nested = str(latest.get("version") or "") if isinstance(latest, dict) else ""
    version = direct or nested
    if not version:
        raise ContractError("Foundry create response omitted its assigned version")
    return version


def _remote_error(payload: bytes) -> tuple[str, str]:
    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "", ""
    error = value.get("error") if isinstance(value, dict) else None
    if not isinstance(error, dict):
        return "", ""
    code = re.sub(r"[^A-Za-z0-9_.-]", "", str(error.get("code") or ""))[:80]
    message = re.sub(r"\s+", " ", str(error.get("message") or "")).strip()[:300]
    return code, message


def _resolve_definition(
    definition: dict[str, Any],
    *,
    project_endpoint: str,
) -> dict[str, Any]:
    value = copy.deepcopy(definition)
    if value.get("kind") == "prompt" and value.get("model") == "gpt-5.4-mini":
        value["model"] = "gpt-5.4-mini"
    environment = value.get("environment_variables")
    if isinstance(environment, dict):
        for key, item in list(environment.items()):
            if item == "${FOUNDRY_PROJECT_ENDPOINT}":
                environment[key] = project_endpoint
    return value
