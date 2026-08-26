from __future__ import annotations

import argparse
import io
import zipfile
from pathlib import Path

import yaml


AGENT_NAME = "finance-agent"
SOURCE_ITEMS = ['source', 'requirements.txt', 'host.yaml']


def is_package_file(path: Path) -> bool:
    return (
        "__pycache__" not in path.parts
        and path.suffix.casefold() not in {".pyc", ".pyo"}
    )


def read_config(path: Path) -> dict:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if value.get("agent_name") != AGENT_NAME:
        raise ValueError(f"configuration is for {value.get('agent_name')}, not {AGENT_NAME}")
    if not value.get("issue_id"):
        raise ValueError("configuration must contain issue_id")
    return value


def add_file(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    archive.writestr(info, data)


def build(issue: Path, output: Path) -> None:
    config = read_config(issue)
    root = Path(__file__).parent
    source_root = issue.parent / "source"
    if not source_root.is_dir():
        raise ValueError(f"source tree is missing beside {issue}")
    entries: list[tuple[str, bytes]] = []
    for item in SOURCE_ITEMS:
        path = source_root if item == "source" else root / item
        if path.is_dir():
            for child in sorted(
                p for p in path.rglob("*") if p.is_file() and is_package_file(p)
            ):
                name = (
                    Path("source") / child.relative_to(source_root)
                    if item == "source"
                    else child.relative_to(root)
                )
                entries.append((name.as_posix(), child.read_bytes()))
        else:
            entries.append((item, path.read_bytes()))
    issue_bytes = yaml.safe_dump(config, sort_keys=False, width=100).encode("utf-8")
    entries.append(("issue.yaml", issue_bytes))
    output.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in sorted(entries):
            add_file(archive, name, data)
    output.write_bytes(buffer.getvalue())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue", type=Path, default=Path("implementation.yaml"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.issue.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()
