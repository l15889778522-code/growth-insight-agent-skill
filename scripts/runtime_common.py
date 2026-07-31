#!/usr/bin/env python3
"""Deterministic helpers shared by the v1.1 workflow scripts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
SCHEMA_DIR = SKILL_ROOT / "schemas"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            if not text.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def atomic_copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    temp_path = Path(temp_name)
    try:
        with source.open("rb") as source_handle, os.fdopen(fd, "wb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        os.replace(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = canonical_json_bytes(value) + b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        remaining = memoryview(line)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError("Unable to append the complete JSONL record.")
            remaining = remaining[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def read_jsonl(path: Path, *, tolerate_truncated_tail: bool = False) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    raw_lines = path.read_bytes().splitlines(keepends=True)
    for index, raw_line in enumerate(raw_lines):
        line_number = index + 1
        if not raw_line.strip():
            continue
        try:
            line = raw_line.decode("utf-8")
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            is_incomplete_tail = index == len(raw_lines) - 1 and not raw_line.endswith((b"\n", b"\r"))
            if tolerate_truncated_tail and is_incomplete_tail:
                break
            raise ValueError(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"Expected JSON object at {path}:{line_number}.")
        records.append(value)
    return records


def atomic_write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    text = "\n".join(canonical_json_bytes(record).decode("utf-8") for record in records)
    atomic_write_text(path, text)


@lru_cache(maxsize=1)
def _schema_registry() -> Registry:
    registry = Registry()
    for path in SCHEMA_DIR.rglob("*.json"):
        resource = Resource.from_contents(load_json(path), default_specification=DRAFT202012)
        registry = registry.with_resource(path.resolve().as_uri(), resource)
    return registry


def schema_errors(instance: Any, schema_path: Path) -> list[str]:
    schema = load_json(schema_path)
    registry = _schema_registry()
    resolver = registry.resolver(base_uri=schema_path.resolve().as_uri())
    validator = Draft202012Validator(schema, registry=registry, _resolver=resolver, format_checker=FormatChecker())
    errors: list[str] = []
    for error in sorted(validator.iter_errors(instance), key=lambda item: list(item.absolute_path)):
        path = ".".join(str(part) for part in error.absolute_path) or "$"
        errors.append(f"{path}: {error.message}")
    return errors


def validate_schema(instance: Any, schema_path: Path) -> None:
    errors = schema_errors(instance, schema_path)
    if errors:
        raise ValueError("Schema validation failed:\n- " + "\n- ".join(errors))


def resolve_within(root: Path, candidate: Path | str) -> Path:
    root = root.resolve()
    path = Path(candidate)
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Path escapes allowed root {root}: {candidate}") from exc
    return resolved


class RunLock(AbstractContextManager["RunLock"]):
    """Cross-platform OS file lock that is automatically released on process exit."""

    def __init__(self, run_dir: Path, timeout_seconds: float = 10.0, stale_seconds: float = 3600.0):
        self.path = run_dir / ".run.lock"
        self.timeout_seconds = timeout_seconds
        self.stale_seconds = stale_seconds  # Retained for call compatibility; OS locks do not need stale takeover.
        self.acquired = False
        self.handle: Any = None

    def _try_lock(self) -> bool:
        assert self.handle is not None
        self.handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        import fcntl

        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def _unlock(self) -> None:
        assert self.handle is not None
        self.handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)

    def __enter__(self) -> "RunLock":
        deadline = time.monotonic() + self.timeout_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b", buffering=0)
        if self.path.stat().st_size == 0:
            self.handle.write(b"\0")
            self.handle.flush()
        while True:
            if self._try_lock():
                self.acquired = True
                payload = canonical_json_bytes({"pid": os.getpid(), "created_at": utc_now()})
                self.handle.seek(0)
                self.handle.truncate()
                self.handle.write(payload)
                self.handle.flush()
                os.fsync(self.handle.fileno())
                return self
            if time.monotonic() >= deadline:
                self.handle.close()
                self.handle = None
                raise TimeoutError(f"Run is locked: {self.path.parent}")
            time.sleep(0.05)

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        try:
            if self.acquired:
                self._unlock()
        finally:
            if self.handle is not None:
                self.handle.close()
            self.handle = None
            self.acquired = False
