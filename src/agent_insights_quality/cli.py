from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from agent_insights_quality.contracts import ContractError, validate_contracts
from agent_insights_quality.docs import generate_documents
from agent_insights_quality.generated_paths import changed_paths, validate_generated_paths
from agent_insights_quality.public_safety import validate_public_repository_content
from agent_insights_quality.security import validate_no_direct_trace_injection


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aiq-quality")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate", help="Validate all repository contracts and generated docs")
    docs_parser = subparsers.add_parser("generate-docs", help="Render manifest-backed documentation")
    docs_parser.add_argument("--check", action="store_true", help="Fail instead of writing stale docs")
    path_parser = subparsers.add_parser(
        "validate-generated-paths",
        help="Enforce the daily automation generated-path allowlist",
    )
    path_parser.add_argument("--base-ref", help="Git ref used to discover changed paths")
    path_parser.add_argument("--path", action="append", default=[], help="Explicit changed path")
    return parser


def run(args: argparse.Namespace) -> None:
    if args.command == "validate":
        validate_contracts()
        generate_documents(check=True)
        validate_no_direct_trace_injection()
        validate_public_repository_content()
        print("Repository contracts are valid.")
    elif args.command == "generate-docs":
        generate_documents(check=args.check)
        print("Generated documentation is current.")
    elif args.command == "validate-generated-paths":
        paths = list(args.path)
        if args.base_ref:
            paths.extend(changed_paths(args.base_ref))
        if not paths:
            raise ContractError("No changed paths supplied")
        validate_generated_paths(paths)
        print("Generated change paths are allowed.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        run(parser.parse_args(argv))
    except ContractError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0
