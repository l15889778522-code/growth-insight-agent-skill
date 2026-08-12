#!/usr/bin/env python3
"""Cross-platform transactional installer for the skill and custom agents."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Sequence

from contracts import CURRENT_CONTRACT_VERSION, SUPPORTED_CONTRACT_VERSIONS


SCHEMA_VERSION = CURRENT_CONTRACT_VERSION
SKILL_NAME = "multi-agent-data-analysis-skill"
SKILL_MANIFEST = f"{SKILL_NAME}.manifest.json"
AGENTS_MANIFEST = "multi-agent-data-analysis-agents.manifest.json"
SUPPORTED_AGENT_CONTRACT_VERSIONS = set(SUPPORTED_CONTRACT_VERSIONS)
SKILL_MUTABLE_PATHS = (".venv", "scripts/__pycache__")
WINDOWS_RESERVED_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}

REQUIRED_AGENT_NAMES = (
    "growth-business",
    "growth-metrics",
    "growth-sql",
    "growth-insight",
    "growth-visualization",
    "growth-review",
    "growth-report",
)

# Keep this list explicit. Developer tests, evaluation fixtures, repository docs,
# and the test harness are intentionally not part of the installed runtime.
SKILL_RUNTIME_FILES = (
    "VERSION",
    "SKILL.md",
    "requirements.txt",
    "constraints.txt",
    "agents/openai.yaml",
    "assets/final-report-template.md",
    "scripts/build_lineage.py",
    "scripts/check_runtime_dependencies.py",
    "scripts/codex_agents_preflight.ps1",
    "scripts/contracts.py",
    "scripts/db_common.py",
    "scripts/evidence.py",
    "scripts/inspect_schema.py",
    "scripts/install_custom_agents.ps1",
    "scripts/install_skill.ps1",
    "scripts/install_skill.py",
    "scripts/render_charts.py",
    "scripts/render_stage_report.py",
    "scripts/run_readonly_query.py",
    "scripts/runctl.py",
    "scripts/runtime_common.py",
    "scripts/sql_guard.py",
    "scripts/test_db_connection.py",
    "scripts/validate_route_plan.py",
    "scripts/validate_stage_output.py",
)


class InstallerError(RuntimeError):
    """An expected installer failure with a user-facing message."""


@dataclass(frozen=True)
class FileRecord:
    path: str
    source: Path
    sha256: str
    size: int

    def manifest_record(self) -> dict[str, object]:
        return {"path": self.path, "sha256": self.sha256, "size": self.size}


@dataclass(frozen=True)
class PackageSpec:
    kind: str
    name: str
    manifest_name: str
    source_root: Path
    files: tuple[FileRecord, ...]
    metadata: dict[str, object]
    mutable_paths: tuple[str, ...] = ()

    @property
    def package_sha256(self) -> str:
        digest = hashlib.sha256()
        for record in self.files:
            digest.update(record.path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(record.sha256.encode("ascii"))
            digest.update(b"\0")
            digest.update(str(record.size).encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest()


@dataclass(frozen=True)
class ExistingManifest:
    payload: dict[str, object]
    files: dict[str, str]
    sizes: dict[str, int | None]


def _absolute(path: str | os.PathLike[str]) -> Path:
    value = os.path.expanduser(os.fspath(path))
    if os.name == "nt" and value.startswith("\\\\?\\"):
        return Path(os.path.normpath(value))
    value = os.path.abspath(value)
    if os.name == "nt":
        if value.startswith("\\\\"):
            value = "\\\\?\\UNC\\" + value[2:]
        else:
            value = "\\\\?\\" + value
    return Path(value)


def _display_path(path: Path) -> str:
    value = str(path)
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def _subprocess_path(path: Path) -> str:
    """Return a native absolute path suitable for Python and pip subprocesses."""

    return _display_path(path) if os.name == "nt" else str(path)


def _windows_volume(path: Path) -> str:
    drive = os.path.splitdrive(str(path))[0]
    if drive.startswith("\\\\?\\UNC\\"):
        drive = "\\\\" + drive[8:]
    elif drive.startswith("\\\\?\\"):
        drive = drive[4:]
    return drive.casefold()


def _is_linklike(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _validate_destination(destination: Path, source_root: Path | None = None) -> None:
    if destination.parent == destination:
        raise InstallerError(f"Refusing to manage a filesystem root as the destination: {destination}")
    if destination == _absolute(Path.home()):
        raise InstallerError(f"Refusing to manage the user home directory as the destination: {destination}")
    if destination == _absolute(tempfile.gettempdir()):
        raise InstallerError(f"Refusing to manage the system temporary directory as the destination: {destination}")
    if destination.exists() and _is_linklike(destination):
        raise InstallerError(f"Destination cannot be a symlink or junction: {destination}")
    if source_root is not None and _is_within(source_root, destination):
        raise InstallerError(f"Destination cannot contain the installer source tree: {destination}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise InstallerError(f"Manifest contains an invalid managed path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
        raise InstallerError(f"Manifest contains an unsafe managed path: {value!r}")
    for part in path.parts:
        stem = part.rstrip(" .").split(".", 1)[0].casefold()
        if ":" in part or part.endswith((" ", ".")) or stem in WINDOWS_RESERVED_NAMES:
            raise InstallerError(f"Manifest path is not portable to Windows: {value!r}")
    return path.as_posix()


def _portable_path_key(value: str) -> str:
    return value.casefold()


def _record(source_root: Path, relative: str) -> FileRecord:
    relative = _safe_relative_path(relative)
    source = source_root.joinpath(*PurePosixPath(relative).parts)
    if _is_linklike(source):
        raise InstallerError(f"Runtime package cannot contain symlinks: {source}")
    if not source.is_file():
        raise InstallerError(f"Required runtime file is missing: {source}")
    try:
        source.resolve().relative_to(source_root.resolve())
    except ValueError as exc:
        raise InstallerError(f"Runtime file escapes the source root: {source}") from exc
    return FileRecord(relative, source, _sha256_file(source), source.stat().st_size)


def _glob_records(source_root: Path, pattern: str) -> list[FileRecord]:
    records = []
    for path in sorted(source_root.glob(pattern), key=lambda item: item.as_posix()):
        if path.is_file():
            records.append(_record(source_root, path.relative_to(source_root).as_posix()))
    if not records:
        raise InstallerError(f"Runtime package pattern matched no files: {pattern}")
    return records


def collect_skill_package(source_root: str | os.PathLike[str]) -> PackageSpec:
    root = _absolute(source_root)
    records = [_record(root, relative) for relative in SKILL_RUNTIME_FILES]
    records.extend(_glob_records(root, "assets/custom-agents/*.toml"))
    records.extend(_glob_records(root, "references/*.md"))
    records.extend(_glob_records(root, "schemas/**/*.json"))
    by_path = {_portable_path_key(record.path): record for record in records}
    if len(by_path) != len(records):
        raise InstallerError("Runtime package inventory contains duplicate paths.")
    return PackageSpec(
        kind="skill",
        name=SKILL_NAME,
        manifest_name=SKILL_MANIFEST,
        source_root=root,
        files=tuple(sorted(by_path.values(), key=lambda item: item.path)),
        metadata={"inventory": "runtime-only"},
        mutable_paths=SKILL_MUTABLE_PATHS,
    )


def _validate_agent_file(record: FileRecord) -> dict[str, object]:
    try:
        content = record.source.read_text(encoding="utf-8")
        parsed = tomllib.loads(content)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise InstallerError(f"Unable to parse custom agent {record.source}: {exc}") from exc
    name = parsed.get("name")
    if name not in REQUIRED_AGENT_NAMES or record.source.stem != name:
        raise InstallerError(f"Unexpected custom agent name {name!r} in {record.source.name}")
    for field in ("description", "developer_instructions", "model_reasoning_effort", "sandbox_mode"):
        if not isinstance(parsed.get(field), str) or not parsed[field].strip():
            raise InstallerError(f"Missing valid {field} in {record.source.name}")
    if parsed["sandbox_mode"] != "read-only":
        raise InstallerError(f"Custom agent must use read-only sandbox mode: {record.source.name}")
    if re.search(r"deepseek|model_provider|DEEPSEEK_API_KEY", content, re.IGNORECASE):
        raise InstallerError(f"External model provider configuration is not allowed: {record.source.name}")
    contract_match = re.search(r'agent_contract_version\s+to\s+"([^"]+)"', content)
    if not contract_match or contract_match.group(1) not in SUPPORTED_AGENT_CONTRACT_VERSIONS:
        raise InstallerError(f"A supported agent contract version is missing: {record.source.name}")
    return {
        "name": name,
        "file": record.path,
        "sha256": record.sha256,
        "model": parsed.get("model"),
        "model_reasoning_effort": parsed["model_reasoning_effort"],
        "sandbox_mode": parsed["sandbox_mode"],
        "agent_contract_version": contract_match.group(1),
    }


def collect_agents_package(source_root: str | os.PathLike[str]) -> PackageSpec:
    root = _absolute(source_root)
    agent_root = root / "assets" / "custom-agents"
    expected_files = {f"{name}.toml" for name in REQUIRED_AGENT_NAMES}
    actual_files = {path.name for path in agent_root.glob("*.toml") if path.is_file()}
    if actual_files != expected_files:
        missing = sorted(expected_files - actual_files)
        unexpected = sorted(actual_files - expected_files)
        raise InstallerError(f"Custom agent inventory differs. Missing: {missing}; unexpected: {unexpected}")
    records = tuple(
        _record(agent_root, f"{name}.toml")
        for name in REQUIRED_AGENT_NAMES
    )
    agents = [_validate_agent_file(record) for record in records]
    found = {str(item["name"]) for item in agents}
    if found != set(REQUIRED_AGENT_NAMES):
        raise InstallerError("Expected exactly the seven canonical custom agents.")
    contract_versions = {str(item["agent_contract_version"]) for item in agents}
    if len(contract_versions) != 1:
        raise InstallerError(f"Custom agents do not share one contract version: {sorted(contract_versions)}")
    contract_version = contract_versions.pop()
    return PackageSpec(
        kind="agents",
        name="multi-agent-data-analysis-agents",
        manifest_name=AGENTS_MANIFEST,
        source_root=agent_root,
        files=tuple(sorted(records, key=lambda item: item.path)),
        metadata={"agent_contract_version": contract_version, "agents": agents},
    )


def _manifest_file_inventory(payload: dict[str, object]) -> tuple[dict[str, str], dict[str, int | None]]:
    raw_files = payload.get("files")
    if raw_files is None and isinstance(payload.get("agents"), list):
        # v1.1 agents manifests stored hashes in the agent records only.
        raw_files = [
            {
                "path": item.get("file"),
                "sha256": item.get("installed_sha256") or item.get("source_sha256") or item.get("sha256"),
            }
            for item in payload["agents"]
            if isinstance(item, dict)
        ]
    if not isinstance(raw_files, list):
        raise InstallerError("Manifest has no valid managed file inventory.")
    result: dict[str, str] = {}
    sizes: dict[str, int | None] = {}
    portable_paths: set[str] = set()
    for item in raw_files:
        if not isinstance(item, dict):
            raise InstallerError("Manifest file inventory contains a non-object entry.")
        path = _safe_relative_path(item.get("path"))
        sha256 = item.get("sha256")
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
            raise InstallerError(f"Manifest contains an invalid SHA-256 for {path}")
        size = item.get("size")
        if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size < 0):
            raise InstallerError(f"Manifest contains an invalid size for {path}")
        portable_key = _portable_path_key(path)
        if path in result or portable_key in portable_paths:
            raise InstallerError(f"Manifest contains a duplicate managed path: {path}")
        result[path] = sha256.lower()
        sizes[path] = size
        portable_paths.add(portable_key)
    return result, sizes


def _read_manifest(path: Path) -> tuple[ExistingManifest | None, str | None]:
    if not path.exists():
        return None, None
    if not path.is_file() or _is_linklike(path):
        return None, f"Manifest is not a regular file: {path}"
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise InstallerError("Manifest root must be an object.")
        files, sizes = _manifest_file_inventory(payload)
        return ExistingManifest(payload, files, sizes), None
    except (OSError, UnicodeError, json.JSONDecodeError, InstallerError) as exc:
        return None, f"Unable to trust existing manifest {path}: {exc}"


def _integrity_errors(destination: Path, files: dict[str, str]) -> list[str]:
    errors = []
    for relative, expected_hash in sorted(files.items()):
        target = destination.joinpath(*PurePosixPath(relative).parts)
        if not target.is_file() or _is_linklike(target):
            errors.append(f"missing or non-regular managed file: {relative}")
        elif _sha256_file(target) != expected_hash:
            errors.append(f"managed file hash mismatch: {relative}")
    return errors


def _expected_install_errors(destination: Path, spec: PackageSpec) -> list[str]:
    errors = []
    for record in spec.files:
        target = destination.joinpath(*PurePosixPath(record.path).parts)
        if not target.is_file() or _is_linklike(target):
            errors.append(f"missing or non-regular runtime file: {record.path}")
        elif _sha256_file(target) != record.sha256:
            errors.append(f"runtime file hash mismatch: {record.path}")
    return errors


def _manifest_matches_spec(manifest: ExistingManifest, spec: PackageSpec, destination: Path) -> bool:
    expected = {record.path: record.sha256 for record in spec.files}
    expected_sizes = {record.path: record.size for record in spec.files}
    return (
        manifest.payload.get("schema_version") == SCHEMA_VERSION
        and manifest.payload.get("package_kind") == spec.kind
        and manifest.payload.get("package_name") == spec.name
        and manifest.payload.get("destination") == _display_path(destination)
        and manifest.payload.get("package_sha256") == spec.package_sha256
        and manifest.payload.get("integrity_scope") == "manifest-managed-files"
        and manifest.payload.get("user_file_policy") == "preserve-unmanaged"
        and manifest.payload.get("mutable_paths") == list(spec.mutable_paths)
        and manifest.files == expected
        and manifest.sizes == expected_sizes
        and all(manifest.payload.get(key) == value for key, value in spec.metadata.items())
    )


def _manifest_payload(spec: PackageSpec, destination: Path, scope: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "package_kind": spec.kind,
        "package_name": spec.name,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "scope": scope,
        "source_root": _display_path(spec.source_root),
        "destination": _display_path(destination),
        "package_sha256": spec.package_sha256,
        "integrity_scope": "manifest-managed-files",
        "user_file_policy": "preserve-unmanaged",
        "mutable_paths": list(spec.mutable_paths),
        "files": [record.manifest_record() for record in spec.files],
    }
    payload.update(spec.metadata)
    return payload


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _nearest_existing(path: Path) -> Path:
    candidate = path
    while not candidate.exists():
        if candidate.parent == candidate:
            raise InstallerError(f"No existing ancestor found for destination: {path}")
        candidate = candidate.parent
    return candidate


def _same_filesystem(left: Path, right: Path) -> bool:
    if os.name == "nt" and _windows_volume(left) != _windows_volume(right):
        return False
    return left.stat().st_dev == right.stat().st_dev


def _stage_candidates(
    destination: Path,
    requested_root: Path | None,
    preferred_roots: Sequence[Path],
) -> list[Path]:
    existing_target = _nearest_existing(destination.parent)
    if requested_root is not None:
        if _is_within(requested_root, destination):
            raise InstallerError("Staging root cannot be inside the destination being replaced.")
        requested_root.mkdir(parents=True, exist_ok=True)
        if not _same_filesystem(requested_root, existing_target):
            raise InstallerError("Staging root must be on the same filesystem as the destination for atomic rename.")
        return [requested_root]

    candidates: list[Path] = []
    temp_root = _absolute(tempfile.gettempdir())
    if temp_root.is_dir():
        candidates.append(temp_root)
    candidates.extend(preferred_roots)
    candidates.append(existing_target)
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in sorted(candidates, key=lambda item: len(str(item))):
        key = os.path.normcase(str(candidate))
        if key in seen or not candidate.is_dir():
            continue
        seen.add(key)
        try:
            if _same_filesystem(candidate, existing_target):
                unique.append(candidate)
        except OSError:
            continue
    return unique


def _make_short_stage(
    destination: Path,
    staging_root: Path | None = None,
    preferred_roots: Sequence[Path] = (),
) -> Path:
    failures = []
    for candidate in _stage_candidates(destination, staging_root, preferred_roots):
        try:
            return Path(tempfile.mkdtemp(prefix=".mada-s-", dir=candidate))
        except OSError as exc:
            failures.append(f"{candidate}: {exc}")
    detail = "; ".join(failures) if failures else "no same-filesystem staging directory was available"
    raise InstallerError(f"Unable to create short staging directory: {detail}")


def _copy_package_to_stage(spec: PackageSpec, stage: Path, destination: Path, scope: str) -> None:
    for record in spec.files:
        target = stage.joinpath(*PurePosixPath(record.path).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(record.source, target)
        if _sha256_file(target) != record.sha256:
            raise InstallerError(f"Staged file failed SHA-256 verification: {record.path}")
    _write_json(stage / spec.manifest_name, _manifest_payload(spec, destination, scope))


def _owned_parent_paths(files: Iterable[str]) -> set[str]:
    parents: set[str] = set()
    for value in files:
        path = PurePosixPath(value)
        for parent in path.parents:
            if parent != PurePosixPath("."):
                parents.add(parent.as_posix())
    return parents


def _preserved_roots(
    destination: Path,
    owned_files: set[str],
    replaced_roots: set[str],
) -> list[str]:
    if not destination.exists():
        return []
    owned_parents = _owned_parent_paths(owned_files)
    preserved: list[str] = []

    def visit(path: Path, relative: str) -> None:
        if relative in replaced_roots or any(relative.startswith(root + "/") for root in replaced_roots):
            return
        if relative in owned_files:
            return
        if path.is_dir() and not _is_linklike(path) and relative in owned_parents:
            for child in sorted(path.iterdir(), key=lambda item: item.name):
                child_relative = f"{relative}/{child.name}" if relative else child.name
                visit(child, child_relative.replace("\\", "/"))
            return
        preserved.append(relative)

    for child in sorted(destination.iterdir(), key=lambda item: item.name):
        visit(child, child.name.replace("\\", "/"))
    return preserved


def _rename(source: Path, destination: Path) -> None:
    """Single rename boundary kept injectable for rollback tests."""
    os.replace(source, destination)


def _remove_tree(path: Path) -> str | None:
    if not path.exists() and not _is_linklike(path):
        return None
    try:
        if path.is_dir() and not _is_linklike(path):
            shutil.rmtree(path)
        else:
            path.unlink()
        return None
    except OSError as exc:
        return str(exc)


def _transactional_replace(
    destination: Path,
    stage: Path,
    preserved: Sequence[str],
    post_commit: Callable[[], None] | None = None,
) -> list[str]:
    backup = stage.parent / f".mada-b-{uuid.uuid4().hex[:8]}"
    moved: list[str] = []
    had_destination = destination.exists()
    committed = False
    try:
        if had_destination:
            _rename(destination, backup)
            for relative in preserved:
                source = backup.joinpath(*PurePosixPath(relative).parts)
                target = stage.joinpath(*PurePosixPath(relative).parts)
                if target.exists() or _is_linklike(target):
                    raise InstallerError(f"Preserved user path collides with the new package: {relative}")
                target.parent.mkdir(parents=True, exist_ok=True)
                _rename(source, target)
                moved.append(relative)
        _rename(stage, destination)
        committed = True
        if post_commit is not None:
            post_commit()
    except BaseException as exc:
        rollback_errors = []
        try:
            if committed and destination.exists():
                _rename(destination, stage)
            if had_destination and backup.exists():
                for relative in reversed(moved):
                    source = stage.joinpath(*PurePosixPath(relative).parts)
                    target = backup.joinpath(*PurePosixPath(relative).parts)
                    if source.exists() or _is_linklike(source):
                        target.parent.mkdir(parents=True, exist_ok=True)
                        _rename(source, target)
                if not destination.exists():
                    _rename(backup, destination)
        except BaseException as rollback_exc:
            rollback_errors.append(str(rollback_exc))
        if not rollback_errors:
            cleanup_error = _remove_tree(stage)
            if cleanup_error:
                rollback_errors.append(f"staging cleanup failed: {cleanup_error}")
        suffix = ""
        if rollback_errors:
            suffix = (
                f" Rollback also reported: {'; '.join(rollback_errors)}. "
                f"Preserved recovery paths: stage={stage}; backup={backup}"
            )
        raise InstallerError(f"Installation transaction failed and rollback was attempted: {exc}.{suffix}") from exc

    warnings = []
    if backup.exists():
        cleanup_error = _remove_tree(backup)
        if cleanup_error:
            warnings.append(f"Old backup could not be removed: {backup}: {cleanup_error}")
    return warnings


def _venv_python(venv: Path) -> Path:
    candidates = (venv / "Scripts" / "python.exe", venv / "bin" / "python")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise InstallerError(f"Virtual environment has no Python executable: {venv}")


def _create_runtime_environment(destination: Path, python_executable: str) -> None:
    venv = destination / ".venv"
    subprocess.run([python_executable, "-m", "venv", _subprocess_path(venv)], check=True)
    command = [
        _subprocess_path(_venv_python(venv)),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "-r",
        _subprocess_path(destination / "requirements.txt"),
    ]
    constraints = destination / "constraints.txt"
    if constraints.is_file():
        command.extend(["-c", _subprocess_path(constraints)])
    subprocess.run(command, check=True)


def install_package(
    spec: PackageSpec,
    destination: str | os.PathLike[str],
    *,
    scope: str = "personal",
    force: bool = False,
    dry_run: bool = False,
    staging_root: str | os.PathLike[str] | None = None,
    install_dependencies: bool = False,
    python_executable: str = sys.executable,
) -> dict[str, object]:
    target = _absolute(destination)
    _validate_destination(target, spec.source_root)
    if target.exists() and not target.is_dir():
        raise InstallerError(f"Destination exists and is not a directory: {target}")
    manifest_path = target / spec.manifest_name
    manifest, manifest_error = _read_manifest(manifest_path)
    expected_errors = _expected_install_errors(target, spec) if target.exists() else ["destination missing"]
    if manifest is not None and not expected_errors and _manifest_matches_spec(manifest, spec, target):
        return {
            "action": "install",
            "component": spec.kind,
            "status": "unchanged",
            "changed": False,
            "destination": _display_path(target),
            "package_sha256": spec.package_sha256,
            "managed_file_count": len(spec.files),
        }

    reasons_requiring_force: list[str] = []
    old_files: dict[str, str] = {}
    if target.exists():
        if manifest_error:
            reasons_requiring_force.append(manifest_error)
        elif manifest is not None:
            old_files = manifest.files
            reasons_requiring_force.extend(_integrity_errors(target, old_files))
        for record in spec.files:
            if record.path in old_files:
                continue
            candidate = target.joinpath(*PurePosixPath(record.path).parts)
            if candidate.exists() or _is_linklike(candidate):
                if not candidate.is_file() or _is_linklike(candidate) or _sha256_file(candidate) != record.sha256:
                    reasons_requiring_force.append(f"unmanaged path collides with runtime file: {record.path}")
    if reasons_requiring_force and not force:
        detail = "; ".join(reasons_requiring_force[:5])
        raise InstallerError(
            f"Installed content cannot be replaced safely: {detail}. "
            "Re-run with --force (-Force in PowerShell) after reviewing local changes."
        )

    try:
        if target.exists() and spec.source_root.samefile(target):
            raise InstallerError("Refusing to rebuild an installed package from itself; run the installer from a source checkout.")
    except FileNotFoundError:
        pass

    owned_files = set(old_files) | {record.path for record in spec.files} | {spec.manifest_name}
    replaced_roots = {"scripts/__pycache__"} if spec.kind == "skill" else set()
    if spec.kind == "skill" and install_dependencies:
        replaced_roots.add(".venv")
    preserved = _preserved_roots(target, owned_files, replaced_roots)
    result: dict[str, object] = {
        "action": "install",
        "component": spec.kind,
        "status": "planned" if dry_run else ("updated" if target.exists() else "installed"),
        "changed": True,
        "dry_run": dry_run,
        "destination": _display_path(target),
        "package_sha256": spec.package_sha256,
        "managed_file_count": len(spec.files),
        "preserved_user_path_count": len(preserved),
        "replaced_modified_content": bool(reasons_requiring_force),
    }
    if dry_run:
        return result

    stage_root = _absolute(staging_root) if staging_root is not None else None
    stage = _make_short_stage(target, stage_root, (spec.source_root,))
    result["staging_root"] = _display_path(stage.parent)
    result["staging_path_length"] = len(str(stage))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_package_to_stage(spec, stage, target, scope)
    except BaseException:
        _remove_tree(stage)
        raise
    post_commit = None
    if spec.kind == "skill" and install_dependencies:
        post_commit = lambda: _create_runtime_environment(target, python_executable)
    warnings = _transactional_replace(target, stage, preserved, post_commit)
    if warnings:
        result["warnings"] = warnings
    return result


def verify_package(spec: PackageSpec, destination: str | os.PathLike[str]) -> dict[str, object]:
    target = _absolute(destination)
    _validate_destination(target, spec.source_root)
    manifest, manifest_error = _read_manifest(target / spec.manifest_name)
    errors = []
    if manifest_error:
        errors.append(manifest_error)
    elif manifest is None:
        errors.append(f"Manifest is missing: {target / spec.manifest_name}")
    else:
        errors.extend(_integrity_errors(target, manifest.files))
        errors.extend(_expected_install_errors(target, spec))
        if not _manifest_matches_spec(manifest, spec, target):
            errors.append("Manifest package metadata does not match this source package.")
    return {
        "action": "verify",
        "component": spec.kind,
        "status": "verified" if not errors else "failed",
        "ok": not errors,
        "destination": _display_path(target),
        "package_sha256": spec.package_sha256,
        "errors": errors,
    }


def uninstall_package(
    destination: str | os.PathLike[str],
    manifest_name: str,
    component: str,
    *,
    force: bool = False,
    dry_run: bool = False,
    purge: bool = False,
    keep_environment: bool = False,
    staging_root: str | os.PathLike[str] | None = None,
) -> dict[str, object]:
    target = _absolute(destination)
    _validate_destination(target)
    manifest, manifest_error = _read_manifest(target / manifest_name)
    if manifest_error:
        raise InstallerError(manifest_error)
    if manifest is None:
        return {
            "action": "uninstall",
            "component": component,
            "status": "absent",
            "changed": False,
            "destination": _display_path(target),
        }
    integrity_errors = _integrity_errors(target, manifest.files)
    if integrity_errors and not force:
        raise InstallerError(
            f"Refusing to remove modified managed content: {'; '.join(integrity_errors[:5])}. "
            "Re-run with --force (-Force in PowerShell) to remove it."
        )
    if purge and not force:
        raise InstallerError("--purge deletes unmanaged user files and therefore also requires --force.")

    owned = set(manifest.files) | {manifest_name}
    replaced_roots = {"scripts/__pycache__"} if component == "skill" else set()
    if component == "skill" and not keep_environment:
        replaced_roots.add(".venv")
    preserved = [] if purge else _preserved_roots(target, owned, replaced_roots)
    result: dict[str, object] = {
        "action": "uninstall",
        "component": component,
        "status": "planned" if dry_run else "uninstalled",
        "changed": True,
        "dry_run": dry_run,
        "destination": _display_path(target),
        "removed_managed_file_count": len(manifest.files),
        "preserved_user_path_count": len(preserved),
        "purged_user_files": purge,
    }
    if dry_run:
        return result

    stage_root = _absolute(staging_root) if staging_root is not None else None
    stage = _make_short_stage(target, stage_root)
    result["staging_root"] = _display_path(stage.parent)
    warnings = _transactional_replace(target, stage, preserved)
    if target.exists() and not any(target.iterdir()):
        target.rmdir()
    if warnings:
        result["warnings"] = warnings
    return result


def skill_destination(scope: str, project_path: str | os.PathLike[str], destination: str | None) -> Path:
    if destination:
        return _absolute(destination)
    if scope == "project":
        return _absolute(project_path) / ".agents" / "skills" / SKILL_NAME
    return Path.home() / ".agents" / "skills" / SKILL_NAME


def agents_destination(scope: str, project_path: str | os.PathLike[str], destination: str | None) -> Path:
    if destination:
        return _absolute(destination)
    if scope == "project":
        return _absolute(project_path) / ".codex" / "agents"
    codex_home = _absolute(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    return codex_home / "agents"


def _add_common_target_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--scope", choices=("personal", "project"), default="personal")
    parser.add_argument("--project-path", default=os.getcwd())
    parser.add_argument("--destination")
    parser.add_argument("--source-root", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--staging-root")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install, verify, or uninstall the multi-agent data analysis runtime.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    install = subparsers.add_parser("install", help="Install or upgrade the Skill runtime.")
    _add_common_target_arguments(install)
    install.add_argument("--install-dependencies", action="store_true")
    install.add_argument("--python-executable", default=sys.executable)

    verify = subparsers.add_parser("verify", help="Verify the Skill against this source checkout.")
    _add_common_target_arguments(verify)

    uninstall = subparsers.add_parser("uninstall", help="Uninstall manifest-managed Skill files.")
    _add_common_target_arguments(uninstall)
    uninstall.add_argument("--purge", action="store_true")
    uninstall.add_argument("--keep-environment", action="store_true")

    install_agents = subparsers.add_parser("install-agents", help="Install or upgrade the seven custom agents.")
    _add_common_target_arguments(install_agents)

    verify_agents = subparsers.add_parser("verify-agents", help="Verify installed custom agents.")
    _add_common_target_arguments(verify_agents)

    uninstall_agents = subparsers.add_parser("uninstall-agents", help="Uninstall manifest-managed custom agents.")
    _add_common_target_arguments(uninstall_agents)
    uninstall_agents.add_argument("--purge", action="store_true")
    return parser


def _execute(args: argparse.Namespace) -> dict[str, object]:
    if args.command in {"install", "verify", "uninstall"}:
        destination = skill_destination(args.scope, args.project_path, args.destination)
        if args.command == "uninstall":
            return uninstall_package(
                destination,
                SKILL_MANIFEST,
                "skill",
                force=args.force,
                dry_run=args.dry_run,
                purge=args.purge,
                keep_environment=args.keep_environment,
                staging_root=args.staging_root,
            )
        spec = collect_skill_package(args.source_root)
        if args.command == "verify":
            return verify_package(spec, destination)
        return install_package(
            spec,
            destination,
            scope=args.scope,
            force=args.force,
            dry_run=args.dry_run,
            staging_root=args.staging_root,
            install_dependencies=args.install_dependencies,
            python_executable=args.python_executable,
        )

    destination = agents_destination(args.scope, args.project_path, args.destination)
    if args.command == "uninstall-agents":
        return uninstall_package(
            destination,
            AGENTS_MANIFEST,
            "agents",
            force=args.force,
            dry_run=args.dry_run,
            purge=args.purge,
            staging_root=args.staging_root,
        )
    spec = collect_agents_package(args.source_root)
    if args.command == "verify-agents":
        return verify_package(spec, destination)
    return install_package(
        spec,
        destination,
        scope=args.scope,
        force=args.force,
        dry_run=args.dry_run,
        staging_root=args.staging_root,
    )


def _print_result(result: dict[str, object], json_output: bool) -> None:
    if json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(f"{str(result['action']).capitalize()} {result['component']}: {result['status']}")
    print(f"Destination: {result['destination']}")
    if result.get("package_sha256"):
        print(f"Package SHA-256: {result['package_sha256']}")
    if result.get("managed_file_count") is not None:
        print(f"Managed files: {result['managed_file_count']}")
    if result.get("preserved_user_path_count") is not None:
        print(f"Preserved user paths: {result['preserved_user_path_count']}")
    for warning in result.get("warnings", []):
        print(f"Warning: {warning}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = _execute(args)
        _print_result(result, args.json_output)
        if result.get("ok") is False:
            return 2
        return 0
    except (InstallerError, OSError, subprocess.CalledProcessError) as exc:
        if getattr(args, "json_output", False):
            print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        else:
            print(f"Installer error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
