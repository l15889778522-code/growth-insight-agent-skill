#!/usr/bin/env python3
"""Deterministic run state, approval, revision, and recovery controller."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tomllib
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from contracts import CURRENT_CONTRACT_VERSION, LEGACY_CONTRACT_VERSIONS
from evidence import artifact_by_id, resolve_evidence_reference
from runtime_common import (
    SCHEMA_DIR,
    RunLock,
    append_jsonl,
    atomic_copy_file,
    atomic_write_json,
    atomic_write_jsonl,
    atomic_write_text,
    load_json,
    new_id,
    read_jsonl,
    resolve_within,
    sha256_bytes,
    sha256_file,
    sha256_json,
    utc_now,
    validate_schema,
)
from validate_route_plan import validate_route
from validate_stage_output import extract_single_json, validate_stage


RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
QUERY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,127}$")
ARTIFACT_KIND_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
STATE_FILE = "run-state.json"
EVENTS_FILE = "events.jsonl"
APPROVALS_FILE = "approvals.jsonl"
EVENT_CHECKPOINT_INTERVAL = 25

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "initialized": {"awaiting_route_confirmation", "stopped"},
    "awaiting_route_confirmation": {"running", "finalizing", "blocked", "stopped", "completed"},
    "running": {"validating", "finalizing", "revising", "awaiting_route_confirmation", "awaiting_query_confirmation", "blocked", "failed", "stopped", "completed"},
    "validating": {"finalizing", "revising", "awaiting_user_confirmation", "awaiting_route_confirmation", "blocked", "failed", "stopped", "completed"},
    "finalizing": {"completed", "failed", "stopped"},
    "awaiting_user_confirmation": {"running", "finalizing", "revising", "awaiting_route_confirmation", "awaiting_query_confirmation", "stopped", "completed"},
    "awaiting_query_confirmation": {"running", "revising", "awaiting_route_confirmation", "blocked", "stopped"},
    "revising": {"running", "awaiting_route_confirmation", "blocked", "failed", "stopped"},
    "blocked": {"running", "revising", "awaiting_route_confirmation", "stopped"},
    "failed": {"revising", "awaiting_route_confirmation", "stopped"},
    "stopped": {"awaiting_route_confirmation", "awaiting_user_confirmation", "awaiting_query_confirmation", "revising", "running", "blocked", "failed", "finalizing"},
    "completed": set(),
}

APPROVAL_ACTIONS = {
    "route": "confirm_route",
    "stage": "approve_stage",
    "query": "execute_query",
    "rollback": "approve_rollback",
}


def _agent_settings() -> tuple[dict[str, str], dict[str, str | None], dict[str, dict[str, Any]]]:
    hashes: dict[str, str] = {}
    models: dict[str, str | None] = {}
    settings: dict[str, dict[str, Any]] = {}
    source = Path(__file__).resolve().parent.parent / "assets" / "custom-agents"
    for path in sorted(source.glob("*.toml")):
        config = tomllib.loads(path.read_text(encoding="utf-8"))
        role = str(config["name"])
        hashes[role] = sha256_file(path)
        models[role] = config.get("model")
        settings[role] = {
            "model": config.get("model"),
            "model_reasoning_effort": config.get("model_reasoning_effort"),
            "sandbox_mode": config.get("sandbox_mode"),
        }
    return hashes, models, settings


def _assert_current_agent_config(state: dict[str, Any], role: str) -> None:
    current_hashes, _, _ = _agent_settings()
    expected = state.get("runtime", {}).get("agent_config_hashes", {}).get(role)
    if not expected or current_hashes.get(role) != expected:
        raise ValueError(f"Agent configuration for {role} changed after the run was initialized.")


def _validate_metric_lineage(value: dict[str, Any]) -> None:
    schema = "metric-lineage-v1.2.schema.json" if value.get("schema_version") == CURRENT_CONTRACT_VERSION else "metric-lineage.schema.json"
    validate_schema(value, SCHEMA_DIR / schema)


def _current_metric_lineage_record(
    run_dir: Path,
    state: dict[str, Any],
    *,
    required: bool = False,
) -> dict[str, Any] | None:
    records = [
        item
        for item in state.get("artifacts", [])
        if item.get("kind") == "metric_lineage_latest" and not item.get("superseded_by")
    ]
    if not records:
        if required:
            raise ValueError("Review requires a current registered metric lineage artifact; build lineage before starting Review.")
        return None
    if len(records) != 1:
        raise ValueError("Exactly one current metric lineage artifact is required.")
    record = records[0]
    path = resolve_within(run_dir, record.get("path", ""))
    if not path.is_file() or sha256_file(path) != record.get("sha256"):
        raise ValueError("Registered metric lineage is missing or changed.")
    _validate_metric_lineage(load_json(path))
    return record


def _review_supporting_artifacts(run_dir: Path, state: dict[str, Any]) -> list[dict[str, Any]]:
    supported_kinds = {
        "metric_lineage_latest",
        "query_manifest",
        "query_result",
        "result_profile",
        "chart_manifest",
    }
    active_artifacts = [item for item in state.get("artifacts", []) if not item.get("superseded_by")]
    active_by_kind: dict[str, list[dict[str, Any]]] = {}
    for item in active_artifacts:
        active_by_kind.setdefault(item.get("kind", ""), []).append(item)

    # A prepared v1.2 query is not usable evidence until its manifest, result,
    # and profile are all published under the same immutable query revision.
    for request_record in active_by_kind.get("query_request", []):
        request_path = resolve_within(run_dir, request_record.get("path", ""))
        if not request_path.is_file() or sha256_file(request_path) != request_record.get("sha256"):
            raise ValueError(f"Query request is missing or changed: {request_record.get('path')}")
        request = load_json(request_path)
        if request.get("schema_version") != CURRENT_CONTRACT_VERSION:
            raise ValueError(f"Query request {request.get('query_id')} is not a current v1.2 request.")
        query_id = request.get("query_id")
        query_revision = request.get("revision")
        if not request.get("metric_ids"):
            raise ValueError(f"Query request {query_id!r} has no metric_ids mapping.")
        manifests = [
            item for item in active_by_kind.get("query_manifest", [])
            if item.get("metadata", {}).get("query_id") == query_id
            and item.get("metadata", {}).get("query_revision") == query_revision
        ]
        if len(manifests) != 1:
            raise ValueError(f"Query {query_id!r} revision {query_revision} requires exactly one query manifest.")
        manifest_record = manifests[0]
        manifest_path = resolve_within(run_dir, manifest_record["path"])
        if not manifest_path.is_file() or sha256_file(manifest_path) != manifest_record["sha256"]:
            raise ValueError(f"Query manifest is missing or changed: {manifest_record['path']}")
        manifest = load_json(manifest_path)
        validate_schema(manifest, SCHEMA_DIR / "query-manifest.schema.json")
        expected = {
            "query_id": query_id,
            "query_revision": query_revision,
            "metric_ids": request.get("metric_ids"),
            "sql_sha256": request.get("sql_sha256"),
            "source_sql_sha256": request.get("source_sql_sha256"),
            "sql_canonicalization": request.get("sql_canonicalization"),
        }
        if any(manifest.get(key) != value for key, value in expected.items()):
            raise ValueError(f"Query manifest for {query_id!r} does not match its approved request.")
        for kind, manifest_key in (("query_result", "result_path"), ("result_profile", "profile_path")):
            matches = [
                item
                for item in active_by_kind.get(kind, [])
                if item.get("path") == manifest.get(manifest_key)
                and item.get("sha256") == manifest.get(manifest_key.replace("_path", "_sha256"))
                and item.get("metadata", {}).get("query_id") == query_id
                and item.get("metadata", {}).get("query_revision") == query_revision
            ]
            if len(matches) != 1:
                raise ValueError(f"Query {query_id!r} revision {query_revision} requires one matching {kind} artifact.")

    records: list[dict[str, Any]] = []
    for artifact in active_artifacts:
        if artifact.get("kind") not in supported_kinds or artifact.get("superseded_by"):
            continue
        path = resolve_within(run_dir, artifact.get("path", ""))
        if not path.is_file() or sha256_file(path) != artifact.get("sha256"):
            raise ValueError(f"Review supporting artifact is missing or changed: {artifact.get('path')}")
        records.append(
            {
                "artifact_id": artifact["artifact_id"],
                "kind": artifact["kind"],
                "path": artifact["path"],
                "sha256": artifact["sha256"],
            }
        )
    return records


def _validate_chart_manifest(value: dict[str, Any]) -> None:
    schema = (
        "chart-render-manifest-v1.2.schema.json"
        if value.get("schema_version") == CURRENT_CONTRACT_VERSION
        else "chart-render-manifest.schema.json"
    )
    validate_schema(value, SCHEMA_DIR / schema)


def _elapsed_ms(started_at: str, completed_at: str) -> int:
    started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    completed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    return max(0, int((completed - started).total_seconds() * 1000))


def _state_path(run_dir: Path) -> Path:
    return run_dir / STATE_FILE


def _event_content_sha256(event: dict[str, Any]) -> str:
    return sha256_json({key: value for key, value in event.items() if key != "event_sha256"})


def _event_chain_sha256(event: dict[str, Any]) -> str:
    return str(event.get("event_sha256") or sha256_json(event))


def _state_delta(before: Any, after: Any, path: tuple[str | int, ...] = ()) -> list[dict[str, Any]]:
    """Return deterministic operations that reconstruct after from before."""
    if type(before) is not type(after):
        return [{"op": "set", "path": list(path), "value": deepcopy(after)}]
    if isinstance(before, dict):
        operations: list[dict[str, Any]] = []
        for key in sorted(set(before) - set(after)):
            operations.append({"op": "remove", "path": [*path, key]})
        for key in sorted(set(before) & set(after)):
            operations.extend(_state_delta(before[key], after[key], (*path, key)))
        for key in sorted(set(after) - set(before)):
            operations.append({"op": "set", "path": [*path, key], "value": deepcopy(after[key])})
        return operations
    if isinstance(before, list):
        operations = []
        for index in range(min(len(before), len(after))):
            operations.extend(_state_delta(before[index], after[index], (*path, index)))
        if len(after) < len(before):
            operations.append({"op": "truncate", "path": list(path), "length": len(after)})
        else:
            for index in range(len(before), len(after)):
                operations.append({"op": "set", "path": [*path, index], "value": deepcopy(after[index])})
        return operations
    if before != after:
        return [{"op": "set", "path": list(path), "value": deepcopy(after)}]
    return []


def _delta_container(root: Any, path: list[str | int]) -> tuple[Any, str | int]:
    if not path:
        raise ValueError("A state-delta operation requires a non-root target.")
    current = root
    for part in path[:-1]:
        if isinstance(current, dict):
            if not isinstance(part, str) or part not in current:
                raise ValueError(f"State-delta path does not exist: {path}")
            current = current[part]
        elif isinstance(current, list):
            if not isinstance(part, int) or isinstance(part, bool) or part < 0 or part >= len(current):
                raise ValueError(f"State-delta list path is invalid: {path}")
            current = current[part]
        else:
            raise ValueError(f"State-delta path crosses a scalar value: {path}")
    return current, path[-1]


def _delta_target(root: Any, path: list[str | int]) -> Any:
    current = root
    for part in path:
        if isinstance(current, dict):
            if not isinstance(part, str) or part not in current:
                raise ValueError(f"State-delta path does not exist: {path}")
            current = current[part]
        elif isinstance(current, list):
            if not isinstance(part, int) or isinstance(part, bool) or part < 0 or part >= len(current):
                raise ValueError(f"State-delta list path is invalid: {path}")
            current = current[part]
        else:
            raise ValueError(f"State-delta path crosses a scalar value: {path}")
    return current


def _apply_state_delta(before: dict[str, Any], operations: Any) -> dict[str, Any]:
    if not isinstance(operations, list):
        raise ValueError("state_delta must be an array of operations.")
    state: Any = deepcopy(before)
    for operation in operations:
        if not isinstance(operation, dict) or not isinstance(operation.get("path"), list):
            raise ValueError("Each state-delta operation requires an object and path array.")
        kind = operation.get("op")
        path = operation["path"]
        if kind == "set" and not path:
            if "value" not in operation:
                raise ValueError("A set operation requires value.")
            state = deepcopy(operation["value"])
            continue
        if kind == "truncate":
            target = _delta_target(state, path)
            length = operation.get("length")
            if (
                not isinstance(target, list)
                or not isinstance(length, int)
                or isinstance(length, bool)
                or length < 0
                or length > len(target)
            ):
                raise ValueError("A truncate operation requires an existing list and a valid shorter length.")
            del target[length:]
            continue
        parent, key = _delta_container(state, path)
        if kind == "set":
            if "value" not in operation:
                raise ValueError("A set operation requires value.")
            value = deepcopy(operation["value"])
            if isinstance(parent, dict) and isinstance(key, str):
                parent[key] = value
            elif isinstance(parent, list) and isinstance(key, int) and not isinstance(key, bool):
                if key == len(parent):
                    parent.append(value)
                elif 0 <= key < len(parent):
                    parent[key] = value
                else:
                    raise ValueError(f"State-delta list set is not contiguous: {path}")
            else:
                raise ValueError(f"State-delta set target is invalid: {path}")
        elif kind == "remove":
            if isinstance(parent, dict) and isinstance(key, str) and key in parent:
                del parent[key]
            elif isinstance(parent, list) and isinstance(key, int) and not isinstance(key, bool) and 0 <= key < len(parent):
                parent.pop(key)
            else:
                raise ValueError(f"State-delta remove target is invalid: {path}")
        else:
            raise ValueError(f"Unsupported state-delta operation: {kind}")
    if not isinstance(state, dict):
        raise ValueError("A replayed run state must be an object.")
    return state


def _event_state_after(event: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    encoding = event.get("state_encoding")
    has_snapshot = "state_after" in event
    has_delta = "state_delta" in event
    if encoding is None:
        if not has_snapshot or has_delta:
            raise ValueError("A legacy event requires exactly one recoverable state snapshot.")
        snapshot = event.get("state_after")
        if not isinstance(snapshot, dict):
            raise ValueError("The recoverable state snapshot is not an object.")
        return deepcopy(snapshot)
    if encoding == "snapshot":
        if not has_snapshot or has_delta or not isinstance(event.get("state_after"), dict):
            raise ValueError("A checkpoint event requires exactly one object state_after.")
        return deepcopy(event["state_after"])
    if encoding == "delta":
        if has_snapshot or not has_delta or previous is None:
            raise ValueError("A delta event requires a previous state and exactly one state_delta.")
        return _apply_state_delta(previous, event["state_delta"])
    raise ValueError(f"Unsupported state encoding: {encoding}")


def _assert_state_head(run_dir: Path, state: dict[str, Any]) -> None:
    events = read_jsonl(run_dir / EVENTS_FILE)
    if not events:
        raise ValueError("Run has no committed event head; run recover before continuing.")
    chain_errors = _audit_event_chain(events)
    if chain_errors:
        raise ValueError("Event chain authentication failed:\n- " + "\n- ".join(chain_errors))
    if state.get("last_event_id") != len(events) or sha256_json(state) != events[-1].get("state_sha256"):
        raise ValueError("run-state.json does not match the last committed event; run recover before continuing.")


def load_state(run_dir: Path, *, authenticate: bool = True, for_update: bool = False) -> dict[str, Any]:
    state = load_json(_state_path(run_dir))
    if not isinstance(state, dict):
        raise ValueError("run-state.json must contain an object.")
    validate_schema(state, SCHEMA_DIR / "run-state.schema.json")
    if authenticate:
        _assert_state_head(run_dir, state)
    if for_update and state.get("schema_version") != CURRENT_CONTRACT_VERSION:
        raise ValueError(
            f"Run contract {state.get('schema_version')!r} is read-only; run migrate before modifying it."
        )
    return state


def _set_status(state: dict[str, Any], target: str) -> None:
    current = state["status"]
    if target == current:
        return
    if target not in ALLOWED_TRANSITIONS.get(current, set()):
        raise ValueError(f"Illegal state transition: {current} -> {target}")
    state["status"] = target


def _commit(run_dir: Path, state: dict[str, Any], event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if state.get("schema_version") != CURRENT_CONTRACT_VERSION and event_type != "contract_migrated":
        raise ValueError("Legacy runs must be explicitly migrated before mutation.")
    previous_path = _state_path(run_dir)
    previous_state = load_json(previous_path) if previous_path.is_file() else None
    if previous_state is not None:
        if not isinstance(previous_state, dict):
            raise ValueError("Persisted run state is not an object.")
        validate_schema(previous_state, SCHEMA_DIR / "run-state.schema.json")
        _assert_state_head(run_dir, previous_state)
        if state.get("revision") != previous_state.get("revision") or state.get("last_event_id") != previous_state.get("last_event_id"):
            raise ValueError("State mutation was not based on the current committed revision.")
    previous_hash = sha256_json(previous_state) if previous_state is not None else None
    state["revision"] += 1
    state["last_event_id"] += 1
    state["updated_at"] = utc_now()
    validate_schema(state, SCHEMA_DIR / "run-state.schema.json")
    state_hash = sha256_json(state)
    events = read_jsonl(run_dir / EVENTS_FILE)
    previous_event_sha256 = _event_chain_sha256(events[-1]) if events else None
    event = {
        "schema_version": CURRENT_CONTRACT_VERSION,
        "event_id": state["last_event_id"],
        "event_type": event_type,
        "run_id": state["run_id"],
        "state_revision": state["revision"],
        "created_at": state["updated_at"],
        "status": state["status"],
        "previous_state_sha256": previous_hash,
        "previous_event_sha256": previous_event_sha256,
        "state_sha256": state_hash,
        "payload": payload or {},
    }
    if previous_state is None or state["last_event_id"] % EVENT_CHECKPOINT_INTERVAL == 0:
        event["state_encoding"] = "snapshot"
        event["state_after"] = deepcopy(state)
    else:
        event["state_encoding"] = "delta"
        event["state_delta"] = _state_delta(previous_state, state)
    event["event_sha256"] = _event_content_sha256(event)
    append_jsonl(run_dir / EVENTS_FILE, event)
    atomic_write_json(_state_path(run_dir), state)
    return event


def initialize_run(runs_root: Path, run_id: str, request: dict[str, Any], model_policy: str = "native-portable") -> Path:
    if not RUN_ID_RE.fullmatch(run_id):
        raise ValueError("run_id must contain 3-128 letters, digits, dots, underscores, or hyphens.")
    run_dir = runs_root.resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    for relative in ("stages", "data", "lineage", "charts", "final"):
        (run_dir / relative).mkdir()
    now = utc_now()
    agent_hashes, resolved_models, agent_settings = _agent_settings()
    request_path = run_dir / "request.json"
    atomic_write_json(request_path, request)
    request_file_sha256 = sha256_file(request_path)
    request_artifact_id = f"request_{request_file_sha256[:16]}"
    state: dict[str, Any] = {
        "schema_version": CURRENT_CONTRACT_VERSION,
        "run_id": run_id,
        "revision": 0,
        "status": "initialized",
        "completion_kind": None,
        "runtime": {
            "kind": "codex_native",
            "model_policy": model_policy,
            "agent_config_hashes": agent_hashes,
            "resolved_models": resolved_models,
            "agent_settings": agent_settings,
            "surface": None,
            "contract_version": CURRENT_CONTRACT_VERSION,
        },
        "route_id": None,
        "route_revision": 0,
        "route_sha256": None,
        "current_stage": None,
        "pending_stage": None,
        "stages": [],
        "approved_artifacts": [],
        "pending_approval": None,
        "pending_query": None,
        "pending_metric_edit": None,
        "execution_leases": [],
        "artifacts": [
            {
                "artifact_id": request_artifact_id,
                "kind": "request",
                "path": "request.json",
                "sha256": request_file_sha256,
                "metadata": {"semantic_sha256": sha256_json(request)},
            }
        ],
        "data_source": None,
        "last_event_id": 0,
        "resume_status": None,
        "created_at": now,
        "updated_at": now,
    }
    _commit(
        run_dir,
        state,
        "run_initialized",
        {
            "request_artifact_id": request_artifact_id,
            "request_file_sha256": request_file_sha256,
            "request_semantic_sha256": sha256_json(request),
        },
    )
    return run_dir


def migrate_run(run_dir: Path) -> dict[str, Any]:
    with RunLock(run_dir):
        state = load_state(run_dir)
        source_version = state.get("schema_version")
        if source_version == CURRENT_CONTRACT_VERSION:
            return state
        if source_version not in LEGACY_CONTRACT_VERSIONS:
            raise ValueError(f"Unsupported run contract version: {source_version!r}")
        if state["status"] == "completed":
            raise ValueError("Completed legacy runs remain read-only; clone the run before migration.")

        request_path = run_dir / "request.json"
        if not request_path.is_file():
            raise ValueError("Legacy run has no request.json to bind during migration.")
        request_hash = sha256_file(request_path)
        if not any(item.get("kind") == "request" for item in state.get("artifacts", [])):
            state["artifacts"].append(
                {
                    "artifact_id": f"request_{request_hash[:16]}",
                    "kind": "request",
                    "path": "request.json",
                    "sha256": request_hash,
                    "metadata": {"migrated_from": source_version},
                }
            )

        stage_artifact_ids: dict[tuple[str, int], str] = {}
        for record in state.get("artifacts", []):
            if "stage_id" not in record:
                continue
            key = (record["stage_id"], int(record["revision"]))
            artifact_id = record.get("artifact_id") or f"legacy_{key[0]}_r{key[1]}"
            record["artifact_id"] = artifact_id
            stage_artifact_ids[key] = artifact_id
        for stage in state.get("stages", []):
            artifact = stage.get("artifact")
            if artifact:
                key = (stage["stage_id"], int(artifact["revision"]))
                artifact["artifact_id"] = stage_artifact_ids.get(key, f"legacy_{key[0]}_r{key[1]}")

        previous_agent_hashes = deepcopy(state["runtime"].get("agent_config_hashes", {}))
        current_agent_hashes, current_models, current_settings = _agent_settings()
        state["schema_version"] = CURRENT_CONTRACT_VERSION
        state["runtime"]["contract_version"] = CURRENT_CONTRACT_VERSION
        state["runtime"]["agent_config_hashes"] = current_agent_hashes
        state["runtime"]["resolved_models"] = current_models
        state["runtime"]["agent_settings"] = current_settings
        state.setdefault("execution_leases", [])
        state.setdefault("completion_kind", None)
        return_state = state
        _commit(
            run_dir,
            state,
            "contract_migrated",
            {
                "from_version": source_version,
                "to_version": CURRENT_CONTRACT_VERSION,
                "previous_agent_config_hashes": previous_agent_hashes,
                "current_agent_config_hashes": current_agent_hashes,
            },
        )
        return return_state


def _find_stage(state: dict[str, Any], stage_id: str) -> dict[str, Any]:
    for stage in state["stages"]:
        if stage["stage_id"] == stage_id:
            return stage
    raise ValueError(f"Unknown stage: {stage_id}")


def _verified_stage_output(run_dir: Path, stage: dict[str, Any]) -> dict[str, Any]:
    artifact = stage.get("artifact")
    if not artifact:
        raise ValueError(f"Stage {stage['stage_id']} has no recorded artifact.")
    paths = {
        "json": (artifact["json_path"], artifact["sha256"]),
        "markdown": (artifact["markdown_path"], artifact["markdown_sha256"]),
        "validation": (artifact["validation_path"], artifact["validation_sha256"]),
    }
    if artifact.get("receipt_path") and artifact.get("receipt_sha256"):
        paths["agent receipt"] = (artifact["receipt_path"], artifact["receipt_sha256"])
    if artifact.get("raw_response_path") and artifact.get("raw_response_sha256"):
        paths["raw response"] = (artifact["raw_response_path"], artifact["raw_response_sha256"])
    resolved: dict[str, Path] = {}
    for label, (relative, expected_hash) in paths.items():
        path = resolve_within(run_dir, relative)
        if not path.is_file():
            raise ValueError(f"Stage {stage['stage_id']} {label} artifact is missing: {relative}")
        if sha256_file(path) != expected_hash:
            raise ValueError(f"Stage {stage['stage_id']} {label} artifact hash changed: {relative}")
        resolved[label] = path
    value = load_json(resolved["json"])
    validation = load_json(resolved["validation"])
    if (
        not isinstance(value, dict)
        or not isinstance(validation, dict)
        or validation.get("valid") is not True
        or validation.get("role") != stage["role"]
        or validation.get("stage_sha256") != sha256_json(value)
        or value.get("stage_status") != artifact.get("stage_status")
    ):
        raise ValueError(f"Stage {stage['stage_id']} artifact and validation report no longer agree.")
    return value


def _load_verified_route(run_dir: Path, state: dict[str, Any], *, require_executable: bool) -> dict[str, Any]:
    route_path = run_dir / "route-plan.json"
    expected_hash = state.get("route_sha256")
    if not route_path.is_file() or not expected_hash or sha256_file(route_path) != expected_hash:
        raise ValueError("Current route file is missing or changed after registration.")
    route = load_json(route_path)
    if route.get("route_id") != state.get("route_id") or route.get("route_revision") != state.get("route_revision"):
        raise ValueError("Current route identity does not match run state.")
    errors, _ = validate_route(route, require_executable=require_executable)
    if errors:
        raise ValueError("Current route is invalid:\n- " + "\n- ".join(errors))
    _validate_route_bindings(run_dir, state, route)
    return route


def _validate_route_bindings(run_dir: Path, state: dict[str, Any], route: dict[str, Any]) -> None:
    if route.get("schema_version") != CURRENT_CONTRACT_VERSION:
        return

    def validate_bindings(container: dict[str, Any], label: str) -> None:
        for binding in container.get("input_bindings", []):
            record = artifact_by_id(state, binding["artifact_id"])
            if record.get("superseded_by"):
                raise ValueError(f"{label} input {binding['input_name']!r} is bound to a superseded artifact.")
            record_path = record.get("path") or record.get("json_path")
            if record_path != binding["path"] or record.get("sha256") != binding["sha256"]:
                raise ValueError(f"{label} input {binding['input_name']!r} does not match its registered artifact.")
            path = resolve_within(run_dir, binding["path"])
            if not path.is_file() or sha256_file(path) != binding["sha256"]:
                raise ValueError(f"{label} input {binding['input_name']!r} artifact is missing or changed.")
            if binding["source"] == "request" and record.get("kind") != "request":
                raise ValueError(f"{label} input {binding['input_name']!r} claims request source for a non-request artifact.")

    validate_bindings(route, "Route")
    if isinstance(route.get("fallback_route"), dict):
        validate_bindings(route["fallback_route"], "Fallback route")


def _next_pending_stage(state: dict[str, Any]) -> str | None:
    for stage in state["stages"]:
        if stage["status"] in {"pending", "revising", "stale"}:
            return stage["stage_id"]
    return None


def _route_has_role(state: dict[str, Any], role: str) -> bool:
    return any(stage.get("role") == role and stage.get("status") != "skipped" for stage in state["stages"])


def _approved_review_stages(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        stage
        for stage in state["stages"]
        if stage["role"] == "growth-review"
        and stage["status"] == "approved"
        and stage.get("artifact")
        and stage["artifact"].get("stage_status") in {"PASS", "PASS_WITH_RISKS"}
        and any(
            approval["stage_id"] == stage["stage_id"]
            and approval["artifact_sha256"] == stage["artifact"]["sha256"]
            for approval in state["approved_artifacts"]
        )
    ]


EDITABLE_METRIC_FIELDS = {
    "name",
    "type",
    "business_definition",
    "formula",
    "grain",
    "dimensions",
    "time_window",
    "field_dependencies",
    "status",
    "source",
    "owner",
    "data_risks",
}


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_array(values: list[Any]) -> str:
    return "[" + ", ".join(_toml_string(item) if isinstance(item, str) else str(item).lower() for item in values) + "]"


def _write_metrics_workbench(path: Path, state: dict[str, Any], stage: dict[str, Any], metrics: list[dict[str, Any]]) -> None:
    lines = [
        "# Human-editable Metrics workbench. Keep metric_id stable; do not edit metadata below.",
        'workbench_version = "1.0"',
        f'contract_version = {_toml_string(CURRENT_CONTRACT_VERSION)}',
        f'run_id = {_toml_string(state["run_id"])}',
        f'stage_id = {_toml_string(stage["stage_id"])}',
        f'base_artifact_sha256 = {_toml_string(stage["artifact"]["sha256"])}',
        "",
    ]
    for metric in metrics:
        lines.append("[[metrics]]")
        for field in (
            "metric_id",
            "name",
            "type",
            "business_definition",
            "formula",
            "grain",
        ):
            lines.append(f"{field} = {_toml_string(metric[field])}")
        lines.append(f"dimensions = {_toml_array(metric['dimensions'])}")
        lines.append(f"time_window = {_toml_string(metric['time_window'])}")
        lines.append(f"field_dependencies = {_toml_array(metric['field_dependencies'])}")
        lines.append(f"status = {_toml_string(metric['status'])}")
        lines.append(f"source = {_toml_string(metric['source'])}")
        lines.append(f"owner = {_toml_string(metric['owner'])}")
        lines.append(f"data_risks = {_toml_array(metric['data_risks'])}")
        lines.append(f"version = {metric['version']}")
        lines.append("")
    atomic_write_text(path, "\n".join(lines).rstrip() + "\n")


def _metric_workbench_payload(path: Path) -> dict[str, Any]:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"Metrics workbench is not valid TOML: {exc}") from exc
    if not isinstance(value, dict) or value.get("workbench_version") != "1.0":
        raise ValueError("Metrics workbench must declare workbench_version = \"1.0\".")
    if value.get("contract_version") != CURRENT_CONTRACT_VERSION:
        raise ValueError("Metrics workbench contract_version does not match the current run contract.")
    metrics = value.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        raise ValueError("Metrics workbench must contain at least one [[metrics]] entry.")
    for metric in metrics:
        if not isinstance(metric, dict):
            raise ValueError("Each [[metrics]] entry must be a TOML table.")
        required = EDITABLE_METRIC_FIELDS | {"metric_id", "version"}
        missing = required - set(metric)
        if missing:
            raise ValueError("Metrics workbench entry is missing field(s): " + ", ".join(sorted(missing)))
        unknown = set(metric) - required
        if unknown:
            raise ValueError("Metrics workbench contains unsupported field(s): " + ", ".join(sorted(unknown)))
        if not isinstance(metric["version"], int) or isinstance(metric["version"], bool) or metric["version"] < 1:
            raise ValueError(f"Metric {metric.get('metric_id')!r} version must be a positive integer.")
        if not isinstance(metric["metric_id"], str) or not re.fullmatch(r"^[a-zA-Z0-9][a-zA-Z0-9._-]+$", metric["metric_id"]):
            raise ValueError(f"Metric ID is invalid: {metric.get('metric_id')!r}.")
        if not isinstance(metric["dimensions"], list) or not all(isinstance(item, str) and item for item in metric["dimensions"]):
            raise ValueError(f"Metric {metric['metric_id']} dimensions must be a string array.")
        if not isinstance(metric["field_dependencies"], list) or not all(isinstance(item, str) and item for item in metric["field_dependencies"]):
            raise ValueError(f"Metric {metric['metric_id']} field_dependencies must be a non-empty string array.")
        if not metric["field_dependencies"]:
            raise ValueError(f"Metric {metric['metric_id']} needs at least one field dependency.")
        if not isinstance(metric["data_risks"], list) or not all(isinstance(item, str) for item in metric["data_risks"]):
            raise ValueError(f"Metric {metric['metric_id']} data_risks must be a string array.")
    ids = [metric["metric_id"] for metric in metrics]
    if len(ids) != len(set(ids)):
        raise ValueError("Metrics workbench contains duplicate metric_id values.")
    names = [str(metric["name"]).casefold() for metric in metrics]
    if len(names) != len(set(names)):
        raise ValueError("Metrics workbench contains duplicate metric names.")
    return value


def _metric_edit_operations(current_metrics: dict[str, dict[str, Any]], proposed_metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    proposed = {item["metric_id"]: item for item in proposed_metrics}
    operations: list[dict[str, Any]] = []
    for metric_id in sorted(set(current_metrics) - set(proposed)):
        operations.append({"operation": "delete", "metric_id": metric_id, "requested_changes": {}})
    for metric_id in sorted(set(proposed) - set(current_metrics)):
        operations.append(
            {
                "operation": "add",
                "metric_id": metric_id,
                "requested_changes": {key: proposed[metric_id][key] for key in EDITABLE_METRIC_FIELDS},
            }
        )
    for metric_id in sorted(set(current_metrics) & set(proposed)):
        changes = {
            key: proposed[metric_id][key]
            for key in EDITABLE_METRIC_FIELDS
            if current_metrics[metric_id].get(key) != proposed[metric_id].get(key)
        }
        if changes:
            operations.append({"operation": "modify", "metric_id": metric_id, "requested_changes": changes})
    return operations


def export_metrics_workbench(run_dir: Path, output: Path) -> Path:
    state = load_state(run_dir)
    stage = next(
        (
            item
            for item in state["stages"]
            if item["role"] == "growth-metrics" and item["status"] == "awaiting_user_confirmation" and item.get("artifact")
        ),
        None,
    )
    if stage is None:
        raise ValueError("Metrics workbench can only be exported while a Metrics artifact awaits confirmation.")
    current = _verified_stage_output(run_dir, stage)
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    _write_metrics_workbench(output, state, stage, current["role_payload"]["metrics"])
    return output


def _build_review_input_bundle(run_dir: Path, state: dict[str, Any], stage: dict[str, Any], path: Path) -> dict[str, Any]:
    ancestor_ids = _descendants_reverse(state, stage["stage_id"])
    inputs: list[dict[str, Any]] = []
    approved_decisions: list[dict[str, Any]] = []
    for prior_stage in state["stages"]:
        if prior_stage["stage_id"] not in ancestor_ids or prior_stage["status"] != "approved" or not prior_stage.get("artifact"):
            continue
        output = _verified_stage_output(run_dir, prior_stage)
        inputs.append(
            {
                "stage_id": prior_stage["stage_id"],
                "role": prior_stage["role"],
                "artifact_id": prior_stage["artifact"]["artifact_id"],
                "artifact_revision": prior_stage["artifact"]["revision"],
                "artifact_sha256": prior_stage["artifact"]["sha256"],
                "json_path": prior_stage["artifact"]["json_path"],
            }
        )
        for decision in output.get("confirmed_decisions", []):
            approved_decisions.append(
                {
                    "stage_id": prior_stage["stage_id"],
                    "artifact_sha256": prior_stage["artifact"]["sha256"],
                    "decision": deepcopy(decision),
                }
            )
    bundle = {
        "schema_version": CURRENT_CONTRACT_VERSION,
        "run_id": state["run_id"],
        "route_revision": state["route_revision"],
        "review_stage_id": stage["stage_id"],
        "inputs": inputs,
        "approved_decisions": approved_decisions,
        "supporting_artifacts": _review_supporting_artifacts(run_dir, state),
        "created_at": utc_now(),
    }
    validate_schema(bundle, SCHEMA_DIR / "review-input-bundle.schema.json")
    atomic_write_json(path, bundle)
    return bundle


def set_route(run_dir: Path, route_file: Path) -> dict[str, Any]:
    route = load_json(route_file)
    if not isinstance(route, dict):
        raise ValueError("Route plan must contain an object.")
    errors, _ = validate_route(route)
    if errors:
        raise ValueError("Invalid route plan:\n- " + "\n- ".join(errors))
    route_bytes = (json.dumps(route, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    route_hash = sha256_bytes(route_bytes)
    with RunLock(run_dir):
        state = load_state(run_dir, for_update=True)
        _validate_route_bindings(run_dir, state, route)
        if state["status"] == "completed":
            raise ValueError("Completed run cannot be rerouted.")
        if state.get("current_stage") or any(item["status"] == "running" for item in state["stages"]):
            raise ValueError("Cannot replace the route while a stage is running.")
        aborted_leases = _abort_active_execution_leases(state, "route_revision")
        saved_route = run_dir / "route-plan.json"
        pending = state.get("pending_approval") or {}
        if (
            state["status"] == "awaiting_route_confirmation"
            and pending.get("approval_type") == "route"
            and pending.get("subject_id") == route["route_id"]
            and pending.get("subject_revision") == route["route_revision"]
            and pending.get("subject_sha256") == route_hash
            and saved_route.is_file()
            and sha256_file(saved_route) == route_hash
        ):
            return state
        if route["route_revision"] <= state["route_revision"]:
            raise ValueError("A new route must use a strictly higher route_revision.")
        if state["status"] != "awaiting_route_confirmation":
            _set_status(state, "awaiting_route_confirmation")
        previous = {item["stage_id"]: item for item in state["stages"]}
        previous_approvals = {item["stage_id"]: item for item in state["approved_artifacts"]}
        reused = {item["stage_id"]: item for item in route.get("reused_approved_artifacts", [])}
        stages: list[dict[str, Any]] = []
        carried_approvals: list[dict[str, Any]] = []
        carried_stage_ids: set[str] = set()
        for item in sorted(route["stages"], key=lambda value: value["sequence"]):
            old = previous.get(item["stage_id"])
            stage_directory = run_dir / "stages" / item["stage_id"]
            if old is None and stage_directory.is_dir() and any(stage_directory.glob("attempt-*")):
                raise ValueError(f"Stage ID {item['stage_id']} was used by an earlier route; use a new stage_id when reintroducing it.")
            reuse = reused.get(item["stage_id"])
            old_approval = previous_approvals.get(item["stage_id"])
            can_reuse = bool(
                old
                and reuse
                and old_approval
                and old["role"] == item["role"]
                and old["status"] == "approved"
                and old["depends_on"] == item["depends_on"]
                and old.get("artifact")
                and old["artifact"]["sha256"] == reuse["artifact_sha256"]
                and old.get("input_hashes", {}) == reuse["input_hashes"]
                and old_approval["artifact_sha256"] == reuse["artifact_sha256"]
                and all(dependency in carried_stage_ids for dependency in item["depends_on"])
            )
            if item["status"] == "approved" and not can_reuse:
                raise ValueError(f"Stage {item['stage_id']} claims approved status without a reusable approved artifact.")
            if reuse and not can_reuse:
                raise ValueError(f"Stage {item['stage_id']} failed approved-artifact reuse checks.")
            if can_reuse:
                _verified_stage_output(run_dir, old)
                carried_approvals.append(old_approval)
                carried_stage_ids.add(item["stage_id"])
            history = deepcopy(old.get("attempt_history", [])) if old else []
            if old and old.get("runtime") and not can_reuse:
                if not any(entry.get("attempt") == old["runtime"].get("attempt") for entry in history):
                    history.append(deepcopy(old["runtime"]))
            stages.append(
                {
                    "stage_id": item["stage_id"],
                    "role": item["role"],
                    "status": "approved" if can_reuse else item["status"],
                    "attempt": old["attempt"] if old else 0,
                    "depends_on": item["depends_on"],
                    "input_hashes": reuse["input_hashes"] if can_reuse else {},
                    "artifact": old["artifact"] if can_reuse else None,
                    "runtime": old.get("runtime") if can_reuse else None,
                    "attempt_history": history,
                }
            )
        atomic_write_json(saved_route, route)
        if sha256_file(saved_route) != route_hash:
            raise ValueError("Persisted route hash does not match the approval fingerprint.")
        non_reused_stage_ids = set(previous) - carried_stage_ids
        if non_reused_stage_ids:
            _invalidate_derived_artifacts(state, non_reused_stage_ids, "route_revision")
        state["route_id"] = route["route_id"]
        state["route_revision"] = route["route_revision"]
        state["route_sha256"] = route_hash
        state["stages"] = stages
        state["approved_artifacts"] = carried_approvals
        state["current_stage"] = None
        state["pending_stage"] = None
        state["pending_query"] = None
        state["pending_metric_edit"] = None
        state["pending_approval"] = {
            "approval_type": "route",
            "subject_id": route["route_id"],
            "subject_revision": route["route_revision"],
            "subject_sha256": route_hash,
        }
        _commit(run_dir, state, "route_proposed", {"route_sha256": route_hash, "aborted_leases": aborted_leases})
        return state


def start_stage(run_dir: Path, stage_id: str) -> Path:
    with RunLock(run_dir):
        state = load_state(run_dir, for_update=True)
        if state["status"] not in {"running", "revising"}:
            raise ValueError(f"Cannot start a stage while run status is {state['status']}.")
        if any(item["status"] == "running" for item in state["stages"]):
            raise ValueError("Another stage is already running.")
        if state.get("pending_approval"):
            raise ValueError("A pending route, stage, or rollback approval must be resolved before a stage can start.")
        if state.get("pending_query"):
            raise ValueError("A prepared query must finish or be revised before another stage can start.")
        route = _load_verified_route(run_dir, state, require_executable=True)
        if state.get("pending_stage") != stage_id:
            raise ValueError(f"The only startable stage is {state.get('pending_stage')!r}.")
        stage = _find_stage(state, stage_id)
        if stage["status"] not in {"pending", "revising", "stale"}:
            raise ValueError(f"Stage {stage_id} cannot start from status {stage['status']}.")
        _assert_current_agent_config(state, stage["role"])
        for dependency in stage["depends_on"]:
            dependency_stage = _find_stage(state, dependency)
            if dependency_stage["status"] != "approved":
                raise ValueError(f"Stage {stage_id} requires approved dependency {dependency}.")
            _verified_stage_output(run_dir, dependency_stage)
            if not any(
                item["stage_id"] == dependency
                and dependency_stage.get("artifact")
                and item["artifact_sha256"] == dependency_stage["artifact"]["sha256"]
                for item in state["approved_artifacts"]
            ):
                raise ValueError(f"Stage {stage_id} dependency {dependency} has no matching approval record.")
        if stage["role"] == "growth-report":
            ancestors = _descendants_reverse(state, stage_id)
            reviews = [item for item in state["stages"] if item["stage_id"] in ancestors and item["role"] == "growth-review"]
            if not any(
                item["status"] == "approved"
                and item.get("artifact", {}).get("stage_status") in {"PASS", "PASS_WITH_RISKS"}
                and any(
                    approved["stage_id"] == item["stage_id"]
                    and approved["artifact_sha256"] == item["artifact"]["sha256"]
                    for approved in state["approved_artifacts"]
                )
                for item in reviews
            ):
                raise ValueError("Report requires an approved Review result of PASS or PASS_WITH_RISKS.")
        if stage["role"] == "growth-review" and any(
            item["role"] == "growth-metrics" and item["status"] in {"approved", "completed"}
            for item in state["stages"]
        ):
            _current_metric_lineage_record(run_dir, state, required=True)
        if stage.get("runtime") and not any(entry.get("attempt") == stage["runtime"].get("attempt") for entry in stage["attempt_history"]):
            stage["attempt_history"].append(deepcopy(stage["runtime"]))
        stage["attempt"] += 1
        stage["status"] = "running"
        settings = state["runtime"]["agent_settings"].get(stage["role"], {})
        stage["runtime"] = {
            "attempt": stage["attempt"],
            "started_at": utc_now(),
            "completed_at": None,
            "elapsed_ms": None,
            "thread_id": None,
            "agent_id": None,
            "model": state["runtime"]["resolved_models"].get(stage["role"]),
            "model_reasoning_effort": settings.get("model_reasoning_effort"),
            "token_usage": None,
            "failure_class": None,
            "receipt_path": None,
            "receipt_sha256": None,
            "raw_response_path": None,
            "raw_response_sha256": None,
            "provenance_trust_level": None,
            "capture_method": None,
            "missing_metadata": [],
        }
        stage["input_hashes"] = {
            dependency: _find_stage(state, dependency)["artifact"]["sha256"]
            for dependency in stage["depends_on"]
            if _find_stage(state, dependency).get("artifact")
        }
        stage["input_hashes"].update(
            {
                f"artifact:{binding['artifact_id']}": binding["sha256"]
                for binding in route.get("input_bindings", [])
            }
        )
        state["current_stage"] = stage_id
        state["pending_stage"] = None
        state["pending_approval"] = None
        attempt_dir = run_dir / "stages" / f"{stage_id}" / f"attempt-{stage['attempt']}"
        attempt_dir.mkdir(parents=True, exist_ok=False)
        _set_status(state, "running")
        _commit(run_dir, state, "stage_started", {"stage_id": stage_id, "role": stage["role"], "attempt": stage["attempt"]})
        if stage["role"] == "growth-review":
            bundle_path = attempt_dir / "review-input-bundle.json"
            _build_review_input_bundle(run_dir, state, stage, bundle_path)
            bundle_record = {
                "artifact_id": new_id("review_input_bundle"),
                "kind": "review_input_bundle",
                "path": str(bundle_path.resolve().relative_to(run_dir.resolve())).replace("\\", "/"),
                "sha256": sha256_file(bundle_path),
                "metadata": {"stage_id": stage_id, "attempt": stage["attempt"]},
            }
            state["artifacts"].append(bundle_record)
            stage["input_hashes"][f"artifact:{bundle_record['artifact_id']}"] = bundle_record["sha256"]
            _commit(
                run_dir,
                state,
                "review_input_bundle_registered",
                {"stage_id": stage_id, "attempt": stage["attempt"], "artifact": bundle_record},
            )
        return attempt_dir


def record_agent_runtime(
    run_dir: Path,
    stage_id: str,
    thread_id: str | None,
    model: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
) -> dict[str, Any]:
    with RunLock(run_dir):
        state = load_state(run_dir, for_update=True)
        stage = _find_stage(state, stage_id)
        if stage["status"] != "running" or not stage.get("runtime"):
            raise ValueError(f"Stage {stage_id} has no active Agent runtime to update.")
        runtime = stage["runtime"]
        if runtime.get("receipt_path"):
            raise ValueError("Agent runtime metadata is immutable after an execution receipt is recorded.")
        runtime["thread_id"] = thread_id
        runtime["model"] = model
        runtime["token_usage"] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens if input_tokens is not None and output_tokens is not None else None,
        }
        state["runtime"]["resolved_models"][stage["role"]] = model
        _commit(
            run_dir,
            state,
            "agent_runtime_recorded",
            {"stage_id": stage_id, "thread_id": thread_id, "model": model, "token_usage": runtime["token_usage"]},
        )
        return runtime


def record_agent_receipt(
    run_dir: Path,
    stage_id: str,
    raw_response_path: Path,
    stage_json_path: Path,
    agent_id: str | None = None,
    model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    capture_method: str = "root_cli",
) -> dict[str, Any]:
    if capture_method not in {"root_cli", "codex_tool_result"}:
        raise ValueError("Agent receipt capture_method must be root_cli or codex_tool_result.")
    if capture_method == "codex_tool_result" and not agent_id:
        raise ValueError("codex_tool_result receipts require the native Agent ID returned by Codex.")
    with RunLock(run_dir):
        state = load_state(run_dir, for_update=True)
        stage = _find_stage(state, stage_id)
        if stage["status"] != "running" or not stage.get("runtime"):
            raise ValueError(f"Stage {stage_id} has no active Agent attempt to receive a receipt.")
        runtime = stage["runtime"]
        if runtime.get("receipt_path"):
            raise ValueError("The active Agent attempt already has an execution receipt.")
        _assert_current_agent_config(state, stage["role"])
        attempt_dir = (run_dir / "stages" / stage_id / f"attempt-{stage['attempt']}").resolve()
        raw_response_path = resolve_within(run_dir, raw_response_path)
        stage_json_path = resolve_within(run_dir, stage_json_path)
        if raw_response_path.parent != attempt_dir or stage_json_path.parent != attempt_dir:
            raise ValueError("Agent receipt files must be inside the active stage attempt directory.")
        if not raw_response_path.is_file() or not stage_json_path.is_file():
            raise ValueError("Agent receipt requires both raw response and parsed stage JSON files.")

        stage_value = load_json(stage_json_path)
        if not isinstance(stage_value, dict):
            raise ValueError("Agent receipt stage JSON must contain an object.")
        raw_value = extract_single_json(raw_response_path.read_text(encoding="utf-8"))
        if sha256_json(raw_value) != sha256_json(stage_value):
            raise ValueError("Raw Agent response does not match the parsed stage JSON.")
        errors = validate_stage(
            stage_value,
            stage["role"],
            state["run_id"],
            stage_id,
            stage["attempt"],
            state["runtime"]["contract_version"],
        )
        if errors:
            raise ValueError("Agent receipt cannot bind an invalid stage response:\n- " + "\n- ".join(errors))

        token_usage = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens if input_tokens is not None and output_tokens is not None else None,
        }
        missing_metadata = []
        if not agent_id:
            missing_metadata.append("agent_id")
        if not model:
            missing_metadata.append("model")
        if input_tokens is None or output_tokens is None:
            missing_metadata.append("token_usage")
        completed_at = utc_now()
        relative_raw = str(raw_response_path.relative_to(run_dir.resolve())).replace("\\", "/")
        relative_stage = str(stage_json_path.relative_to(run_dir.resolve())).replace("\\", "/")
        receipt = {
            "schema_version": CURRENT_CONTRACT_VERSION,
            "receipt_id": new_id("agent_receipt"),
            "run_id": state["run_id"],
            "stage_id": stage_id,
            "role": stage["role"],
            "attempt": stage["attempt"],
            "agent_config_sha256": state["runtime"]["agent_config_hashes"][stage["role"]],
            "agent_id": agent_id,
            "raw_response_path": relative_raw,
            "raw_response_sha256": sha256_file(raw_response_path),
            "stage_json_path": relative_stage,
            "stage_json_sha256": sha256_file(stage_json_path),
            "stage_semantic_sha256": sha256_json(stage_value),
            "started_at": runtime["started_at"],
            "completed_at": completed_at,
            "model": model,
            "model_reasoning_effort": runtime.get("model_reasoning_effort"),
            "token_usage": token_usage,
            "missing_metadata": missing_metadata,
            "capture_method": capture_method,
            "trust_level": "host_observed" if capture_method == "codex_tool_result" else "self_asserted",
            "host_receipt": None,
            "created_at": completed_at,
        }
        validate_schema(receipt, SCHEMA_DIR / "agent-execution-receipt.schema.json")
        receipt_path = attempt_dir / "agent-execution-receipt.json"
        atomic_write_json(receipt_path, receipt)
        relative_receipt = str(receipt_path.relative_to(run_dir.resolve())).replace("\\", "/")
        runtime.update(
            {
                "thread_id": agent_id,
                "agent_id": agent_id,
                "model": model,
                "token_usage": token_usage,
                "receipt_path": relative_receipt,
                "receipt_sha256": sha256_file(receipt_path),
                "raw_response_path": relative_raw,
                "raw_response_sha256": receipt["raw_response_sha256"],
                "provenance_trust_level": receipt["trust_level"],
                "capture_method": capture_method,
                "missing_metadata": missing_metadata,
            }
        )
        state["runtime"]["resolved_models"][stage["role"]] = model
        _commit(
            run_dir,
            state,
            "agent_execution_receipt_recorded",
            {
                "stage_id": stage_id,
                "attempt": stage["attempt"],
                "receipt_id": receipt["receipt_id"],
                "receipt_sha256": runtime["receipt_sha256"],
                "agent_id": agent_id,
                "trust_level": receipt["trust_level"],
            },
        )
        return receipt


def _verify_agent_receipt(
    run_dir: Path,
    state: dict[str, Any],
    stage: dict[str, Any],
    stage_json_path: Path,
    stage_value: dict[str, Any],
) -> dict[str, Any]:
    runtime = stage.get("runtime") or {}
    receipt_relative = runtime.get("receipt_path")
    receipt_sha256 = runtime.get("receipt_sha256")
    if not receipt_relative or not receipt_sha256:
        raise ValueError("Stage has no Agent execution receipt for the active attempt.")
    receipt_path = resolve_within(run_dir, receipt_relative)
    if not receipt_path.is_file() or sha256_file(receipt_path) != receipt_sha256:
        raise ValueError("Agent execution receipt is missing or changed.")
    receipt = load_json(receipt_path)
    validate_schema(receipt, SCHEMA_DIR / "agent-execution-receipt.schema.json")
    expected = {
        "run_id": state["run_id"],
        "stage_id": stage["stage_id"],
        "role": stage["role"],
        "attempt": stage["attempt"],
        "agent_config_sha256": state["runtime"]["agent_config_hashes"][stage["role"]],
        "stage_json_sha256": sha256_file(stage_json_path),
        "stage_semantic_sha256": sha256_json(stage_value),
    }
    for key, value in expected.items():
        if receipt.get(key) != value:
            raise ValueError(f"Agent execution receipt {key} does not match the active stage attempt.")
    raw_path = resolve_within(run_dir, receipt["raw_response_path"])
    if not raw_path.is_file() or sha256_file(raw_path) != receipt["raw_response_sha256"]:
        raise ValueError("Raw Agent response is missing or changed.")
    raw_value = extract_single_json(raw_path.read_text(encoding="utf-8"))
    if sha256_json(raw_value) != sha256_json(stage_value):
        raise ValueError("Raw Agent response no longer matches the stage JSON.")
    if receipt.get("trust_level") == "host_signed" and not receipt.get("host_receipt"):
        raise ValueError("Agent receipt claims host_signed without a verifiable host receipt.")
    expected_missing = []
    if not receipt.get("agent_id"):
        expected_missing.append("agent_id")
    if not receipt.get("model"):
        expected_missing.append("model")
    usage = receipt.get("token_usage") or {}
    if usage.get("input_tokens") is None or usage.get("output_tokens") is None:
        expected_missing.append("token_usage")
    if receipt.get("missing_metadata") != expected_missing:
        raise ValueError("Agent receipt missing_metadata does not match the metadata actually present.")
    return receipt


def _load_current_role_output(run_dir: Path, state: dict[str, Any], role: str) -> dict[str, Any] | None:
    candidates = [
        item
        for item in state["stages"]
        if item["role"] == role and item.get("artifact") and item["status"] in {"approved", "completed"}
    ]
    if not candidates:
        return None
    return _verified_stage_output(run_dir, candidates[-1])


def _current_query_quality_warnings(
    run_dir: Path,
    state: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    latest_by_query: dict[str, tuple[int, dict[str, Any]]] = {}
    seen_revisions: set[tuple[str, int]] = set()
    errors: list[str] = []
    for artifact in state["artifacts"]:
        if artifact.get("kind") != "query_manifest" or artifact.get("superseded_by"):
            continue
        metadata = artifact.get("metadata", {})
        query_id = metadata.get("query_id")
        query_revision = metadata.get("query_revision")
        if not isinstance(query_id, str) or not query_id or not isinstance(query_revision, int) or query_revision < 1:
            errors.append(f"Query manifest {artifact.get('artifact_id')} has invalid query identity metadata.")
            continue
        key = (query_id, query_revision)
        if key in seen_revisions:
            errors.append(f"Query {query_id} has duplicate active manifest revision {query_revision}.")
            continue
        seen_revisions.add(key)
        prior = latest_by_query.get(query_id)
        if prior is None or query_revision > prior[0]:
            latest_by_query[query_id] = (query_revision, artifact)

    warnings: list[dict[str, Any]] = []
    for query_id in sorted(latest_by_query):
        query_revision, artifact = latest_by_query[query_id]
        try:
            path = resolve_within(run_dir, artifact["path"])
            if not path.is_file() or sha256_file(path) != artifact["sha256"]:
                raise ValueError(f"Latest query manifest is missing or changed: {artifact['path']}")
            manifest = load_json(path)
            validate_schema(manifest, SCHEMA_DIR / "query-manifest.schema.json")
            if manifest.get("query_id") != query_id:
                raise ValueError(f"Query manifest {artifact['artifact_id']} disagrees with its query identity metadata.")
            for warning in manifest["quality_warnings"]:
                warnings.append(
                    {
                        "artifact_id": artifact["artifact_id"],
                        "sha256": artifact["sha256"],
                        "query_id": query_id,
                        "query_revision": query_revision,
                        "warning": warning,
                    }
                )
        except (KeyError, OSError, ValueError) as exc:
            errors.append(str(exc))
    return warnings, errors


def _validate_stage_context(run_dir: Path, state: dict[str, Any], stage: dict[str, Any], value: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for dependency, expected_hash in stage["input_hashes"].items():
        if dependency.startswith("artifact:"):
            artifact_id = dependency.removeprefix("artifact:")
            try:
                current = artifact_by_id(state, artifact_id)
            except ValueError as exc:
                errors.append(str(exc))
                continue
            path_value = current.get("path") or current.get("json_path")
            try:
                path = resolve_within(run_dir, path_value)
                if current.get("sha256") != expected_hash or not path.is_file() or sha256_file(path) != expected_hash:
                    errors.append(f"Bound route input {artifact_id} changed while the stage was running.")
            except (TypeError, ValueError) as exc:
                errors.append(str(exc))
            continue
        current = _find_stage(state, dependency).get("artifact")
        if not current or current["sha256"] != expected_hash:
            errors.append(f"Approved input {dependency} changed while the stage was running.")

    ancestor_ids = _descendants_reverse(state, stage["stage_id"])

    if stage["role"] == "growth-review":
        bundles = [
            item
            for item in state["artifacts"]
            if item.get("kind") == "review_input_bundle"
            and item.get("metadata", {}).get("stage_id") == stage["stage_id"]
            and item.get("metadata", {}).get("attempt") == stage["attempt"]
            and not item.get("superseded_by")
        ]
        if len(bundles) != 1:
            errors.append("Review requires exactly one immutable input bundle for the active attempt.")
        else:
            bundle_record = bundles[0]
            try:
                bundle_path = resolve_within(run_dir, bundle_record["path"])
                if not bundle_path.is_file() or sha256_file(bundle_path) != bundle_record["sha256"]:
                    raise ValueError("Review input bundle is missing or changed.")
                bundle = load_json(bundle_path)
                validate_schema(bundle, SCHEMA_DIR / "review-input-bundle.schema.json")
                if bundle["run_id"] != state["run_id"] or bundle["route_revision"] != state["route_revision"] or bundle["review_stage_id"] != stage["stage_id"]:
                    raise ValueError("Review input bundle identity does not match the current run.")
                expected_inputs = []
                expected_decisions = []
                for prior_stage in state["stages"]:
                    if prior_stage["stage_id"] not in ancestor_ids or prior_stage["status"] != "approved" or not prior_stage.get("artifact"):
                        continue
                    output = _verified_stage_output(run_dir, prior_stage)
                    expected_inputs.append(
                        {
                            "stage_id": prior_stage["stage_id"],
                            "role": prior_stage["role"],
                            "artifact_id": prior_stage["artifact"]["artifact_id"],
                            "artifact_revision": prior_stage["artifact"]["revision"],
                            "artifact_sha256": prior_stage["artifact"]["sha256"],
                            "json_path": prior_stage["artifact"]["json_path"],
                        }
                    )
                    for decision in output.get("confirmed_decisions", []):
                        expected_decisions.append(
                            {
                                "stage_id": prior_stage["stage_id"],
                                "artifact_sha256": prior_stage["artifact"]["sha256"],
                                "decision": decision,
                            }
                        )
                bundled_supporting = bundle.get("supporting_artifacts", [])
                for supporting in bundled_supporting:
                    registered = artifact_by_id(state, supporting["artifact_id"])
                    registered_path = registered.get("path") or registered.get("json_path")
                    if (
                        registered.get("kind") != supporting["kind"]
                        or registered_path != supporting["path"]
                        or registered.get("sha256") != supporting["sha256"]
                    ):
                        raise ValueError(
                            f"Review supporting artifact binding changed: {supporting['artifact_id']}"
                        )
                    supporting_path = resolve_within(run_dir, supporting["path"])
                    if not supporting_path.is_file() or sha256_file(supporting_path) != supporting["sha256"]:
                        raise ValueError(
                            f"Review supporting artifact is missing or changed: {supporting['path']}"
                        )
                expected_supporting = _review_supporting_artifacts(run_dir, state) if stage["status"] == "running" else bundled_supporting
                if (
                    bundle["inputs"] != expected_inputs
                    or bundle["approved_decisions"] != expected_decisions
                    or bundled_supporting != expected_supporting
                ):
                    errors.append("Review input bundle no longer matches the approved upstream artifacts.")
            except (OSError, KeyError, ValueError) as exc:
                errors.append(f"Review input bundle validation failed: {exc}")

    for artifact in value.get("data_artifacts", []):
        digest = artifact.get("sha256")
        if digest is None:
            continue
        try:
            path = resolve_within(run_dir, artifact["path"])
            if not path.is_file():
                errors.append(f"Referenced data artifact does not exist: {artifact['path']}")
            elif sha256_file(path) != digest:
                errors.append(f"Referenced data artifact hash changed: {artifact['path']}")
        except ValueError as exc:
            errors.append(str(exc))

    role = stage["role"]
    payload = value.get("role_payload", {})
    metric_output = _load_current_role_output(run_dir, state, "growth-metrics")
    known_metrics = {
        item["metric_id"]
        for item in (metric_output or {}).get("role_payload", {}).get("metrics", [])
    }

    if role == "growth-metrics":
        metrics = payload.get("metrics", [])
        ids = [item.get("metric_id") for item in metrics]
        if len(ids) != len(set(ids)):
            errors.append("Metrics output contains duplicate metric_id values.")
        common = {(item.get("metric_id"), item.get("name"), item.get("version")) for item in value.get("metrics", [])}
        specialized = {(item.get("metric_id"), item.get("name"), item.get("version")) for item in metrics}
        if common != specialized:
            errors.append("Common metrics must mirror role_payload.metrics IDs, names, and versions.")
        prior_paths = [
            item["json_path"]
            for item in state["artifacts"]
            if item.get("stage_id") == stage["stage_id"] and item.get("json_path")
        ]
        if prior_paths:
            historical_by_id: dict[str, dict[str, Any]] = {}
            verified_priors: list[dict[str, Any]] = []
            for prior_path in prior_paths:
                resolved_prior = resolve_within(run_dir, prior_path)
                prior_record = next(
                    item
                    for item in state["artifacts"]
                    if item.get("stage_id") == stage["stage_id"] and item.get("json_path") == prior_path
                )
                if not resolved_prior.is_file() or sha256_file(resolved_prior) != prior_record["sha256"]:
                    errors.append(f"Historical Metrics artifact hash changed: {prior_path}")
                    continue
                prior = load_json(resolved_prior)
                verified_priors.append(prior)
                historical_by_id.update({item["metric_id"]: item for item in prior.get("role_payload", {}).get("metrics", [])})
            latest = verified_priors[-1] if verified_priors else {"role_payload": {"metrics": []}}
            old_by_id = {item["metric_id"]: item for item in latest.get("role_payload", {}).get("metrics", [])}
            for metric in metrics:
                old = old_by_id.get(metric["metric_id"])
                if old is None:
                    historical = historical_by_id.get(metric["metric_id"])
                    expected_version = historical["version"] + 1 if historical else 1
                    if metric["version"] != expected_version:
                        label = "Reintroduced" if historical else "New"
                        errors.append(f"{label} metric {metric['metric_id']} must use version {expected_version}.")
                    continue
                old_content = {key: item for key, item in old.items() if key != "version"}
                new_content = {key: item for key, item in metric.items() if key != "version"}
                expected_version = old["version"] + 1 if old_content != new_content else old["version"]
                if metric["version"] != expected_version:
                    errors.append(f"Metric {metric['metric_id']} must use version {expected_version} for this revision.")
        pending_edit = state.get("pending_metric_edit")
        if pending_edit and pending_edit.get("stage_id") == stage["stage_id"]:
            if not stage.get("artifact") or stage["artifact"]["sha256"] != pending_edit["base_artifact_sha256"]:
                errors.append("Pending metric edit is not based on the prior Metrics artifact.")
            edit_path = resolve_within(run_dir, pending_edit["edit_path"])
            if not edit_path.is_file() or sha256_file(edit_path) != pending_edit["edit_sha256"]:
                errors.append("Pending metric edit file is missing or changed.")
            else:
                edit = load_json(edit_path)
                try:
                    validate_schema(edit, SCHEMA_DIR / "metric-edit.schema.json")
                except ValueError as exc:
                    errors.append(str(exc))
                by_id = {item["metric_id"]: item for item in metrics}
                metric_id = edit.get("metric_id")
                operation = edit.get("operation")
                if operation == "batch":
                    requested = edit.get("requested_changes", {})
                    expected_metrics = requested.get("metrics") if isinstance(requested, dict) else None
                    if not isinstance(expected_metrics, list):
                        errors.append("Batch metric edit is missing requested_changes.metrics.")
                    elif metrics != expected_metrics:
                        errors.append("Metrics workbench was not applied exactly; the revised metric list differs from the workbench.")
                    operation_records = requested.get("operations", []) if isinstance(requested, dict) else []
                    if operation_records != _metric_edit_operations(
                        {item["metric_id"]: item for item in latest.get("role_payload", {}).get("metrics", [])},
                        expected_metrics or [],
                    ):
                        errors.append("Batch metric edit operations do not match the workbench diff.")
                if operation == "delete" and metric_id in by_id:
                    errors.append(f"Metric edit requested deletion of {metric_id}, but it remains in the revised artifact.")
                if operation in {"add", "modify"}:
                    revised_metric = by_id.get(metric_id)
                    if revised_metric is None:
                        errors.append(f"Metric edit requested {operation} for {metric_id}, but the revised metric is absent.")
                    else:
                        mismatches = [
                            key
                            for key, expected in edit.get("requested_changes", {}).items()
                            if revised_metric.get(key) != expected
                        ]
                        if mismatches:
                            errors.append("Metric edit was not applied for field(s): " + ", ".join(sorted(mismatches)))

    if role == "growth-sql" and known_metrics:
        mappings = payload.get("field_mappings", [])
        queries = payload.get("queries", [])
        unsupported = payload.get("unsupported_metrics", [])
        mapping_ids = [mapping.get("metric_id") for mapping in mappings]
        query_ids = [metric_id for query in queries for metric_id in query.get("metric_ids", [])]
        referenced = mapping_ids + query_ids + unsupported
        unknown = sorted({item for item in referenced if item not in known_metrics})
        if unknown:
            errors.append("SQL output references unknown metric IDs: " + ", ".join(unknown))
        # New registration rules must not retroactively invalidate immutable v1.2 artifacts.
        if stage["status"] == "running":
            if len(unsupported) != len(set(unsupported)):
                errors.append("SQL unsupported_metrics contains duplicate metric IDs.")
            accounted = set(mapping_ids) | set(unsupported)
            missing = sorted(known_metrics - accounted)
            if missing:
                errors.append("SQL output did not map or explicitly mark unsupported metric IDs: " + ", ".join(missing))
            supported_ids = {
                mapping.get("metric_id")
                for mapping in mappings
                if mapping.get("supported")
            }
            unsupported_mapping_ids = {
                mapping.get("metric_id")
                for mapping in mappings
                if not mapping.get("supported")
            }
            undeclared_unsupported = sorted(unsupported_mapping_ids - set(unsupported))
            if undeclared_unsupported:
                errors.append(
                    "Unsupported SQL field mappings must also appear in unsupported_metrics: "
                    + ", ".join(undeclared_unsupported)
                )
            overlap = sorted(supported_ids & set(unsupported))
            if overlap:
                errors.append("SQL metrics cannot be both supported and unsupported: " + ", ".join(overlap))
            for mapping in mappings:
                metric_id = mapping.get("metric_id")
                if mapping.get("supported") and (
                    not mapping.get("source_fields")
                    or not mapping.get("sql_expression")
                    or not mapping.get("result_column")
                ):
                    errors.append(f"Supported SQL metric {metric_id} requires source fields, SQL expression, and result column.")
            executable_metric_ids = {
                metric_id
                for query in queries
                if query.get("executable")
                for metric_id in query.get("metric_ids", [])
            }
            missing_queries = sorted(supported_ids - executable_metric_ids)
            if missing_queries:
                errors.append("Supported SQL metrics are absent from executable queries: " + ", ".join(missing_queries))
            unsupported_queries = sorted(set(unsupported) & executable_metric_ids)
            if unsupported_queries:
                errors.append("Unsupported SQL metrics cannot appear in executable queries: " + ", ".join(unsupported_queries))

    if role in {"growth-insight", "growth-report"} and known_metrics:
        collection = payload.get("observations", []) if role == "growth-insight" else payload.get("recommendations", [])
        referenced = {metric_id for item in collection for metric_id in item.get("metric_ids", [])}
        unknown = sorted(referenced - known_metrics)
        if unknown:
            errors.append(f"{role} output references unknown metric IDs: " + ", ".join(unknown))

    data_records = [
        item
        for item in state["artifacts"]
        if item.get("kind") in {"query_result", "user_result"} and not item.get("superseded_by")
    ]
    for record in data_records:
        try:
            data_path = resolve_within(run_dir, record["path"])
            if not data_path.is_file() or sha256_file(data_path) != record["sha256"]:
                errors.append(f"Registered result artifact is missing or changed: {record['path']}")
        except (KeyError, ValueError) as exc:
            errors.append(str(exc))
    evidence_items = []
    if role == "growth-insight":
        evidence_items.extend(payload.get("observations", []))
        evidence_items.extend(payload.get("recommendations", []))
    elif role == "growth-review":
        evidence_items.extend(payload.get("findings", []))
    elif role == "growth-report":
        evidence_items.extend(payload.get("recommendations", []))
    evidence_references: list[Any] = list(value.get("evidence", []))
    for calculation in value.get("calculations", []):
        if isinstance(calculation, dict):
            evidence_references.extend(calculation.get("input_evidence_refs", []))
    for item in evidence_items:
        evidence_references.extend(item.get("evidence_refs", []))
    for reference in evidence_references:
        try:
            resolve_evidence_reference(
                run_dir,
                state,
                reference,
                allow_legacy=value.get("schema_version") in LEGACY_CONTRACT_VERSIONS,
            )
        except (OSError, ValueError) as exc:
            errors.append(f"{role} output has an unresolved evidence reference: {exc}")

    if role == "growth-insight" and not data_records:
        if value.get("facts") or value.get("calculations") or payload.get("observations"):
            errors.append("Insight without a registered result file may contain hypotheses and validation steps only.")

    if role == "growth-visualization":
        source_file = payload.get("source_file")
        source_hash = payload.get("source_sha256")
        charts = payload.get("chart_specs", [])
        if source_file is None or source_hash is None:
            if source_file is not None or source_hash is not None:
                errors.append("Visualization source_file and source_sha256 must both be set or both be null.")
            if any(item.get("status") != "blocked_by_missing_data" for item in charts):
                errors.append("Visualization without real data must block every chart specification.")
        else:
            try:
                source = resolve_within(run_dir, source_file)
                if not source.is_file() or sha256_file(source) != source_hash:
                    errors.append("Visualization source file or hash does not match a real run artifact.")
                elif not any(item.get("path") == source_file and item.get("sha256") == source_hash for item in data_records):
                    errors.append("Visualization source must be a registered query_result or user_result artifact.")
            except ValueError as exc:
                errors.append(str(exc))
        if known_metrics:
            unknown = sorted({item.get("metric_id") for item in charts if item.get("metric_id") not in known_metrics})
            if unknown:
                errors.append("Visualization output references unknown metric IDs: " + ", ".join(unknown))

    if role == "growth-review":
        decision = payload.get("decision")
        expected_quality_warnings, quality_errors = _current_query_quality_warnings(run_dir, state)
        errors.extend(quality_errors)
        if payload.get("data_quality_warnings", []) != expected_quality_warnings:
            errors.append(
                "Review data_quality_warnings must exactly match the latest registered query manifests: "
                + json.dumps(expected_quality_warnings, ensure_ascii=False, sort_keys=True)
            )
        metric_stages = [
            item
            for item in state["stages"]
            if item["role"] == "growth-metrics" and item["status"] in {"approved", "completed"}
        ]
        if metric_stages:
            try:
                lineage_record = _current_metric_lineage_record(run_dir, state, required=True)
                lineage = load_json(resolve_within(run_dir, lineage_record["path"]))
                expected_breaks = {
                    f"{metric['metric_id']}:{lineage_break}"
                    for metric in lineage.get("metrics", [])
                    for lineage_break in metric.get("breaks", [])
                }
                declared_breaks = set(payload.get("lineage_breaks", []))
                if declared_breaks != expected_breaks:
                    errors.append("Review lineage_breaks must exactly match the registered metric lineage.")
            except ValueError as exc:
                errors.append(str(exc))
        if decision in {"PASS", "PASS_WITH_RISKS"} and payload.get("lineage_breaks"):
            errors.append("Review cannot pass while metric lineage breaks remain; repair the earliest responsible stage.")
        rollback = payload.get("rollback_stage")
        if rollback is not None and rollback not in {item["stage_id"] for item in state["stages"]}:
            errors.append(f"Review rollback_stage is not in the current route: {rollback}")
        elif rollback is not None and rollback not in ancestor_ids:
            errors.append(f"Review rollback_stage must be an ancestor of the Review stage: {rollback}")
        unknown_responsible = sorted(
            {
                item["responsible_stage"]
                for item in payload.get("findings", [])
                if item.get("responsible_stage") is not None
                and item["responsible_stage"] not in {stage_item["stage_id"] for stage_item in state["stages"]}
            }
        )
        if unknown_responsible:
            errors.append("Review findings reference unknown responsible stages: " + ", ".join(unknown_responsible))

    if role == "growth-report":
        review = _load_current_role_output(run_dir, state, "growth-review")
        if not review or review.get("stage_status") not in {"PASS", "PASS_WITH_RISKS"}:
            errors.append("Report input does not include an approved passing Review artifact.")
        else:
            required_caveats = [item for item in review.get("risks", []) if isinstance(item, str)]
            if review.get("role_payload", {}).get("decision") == "PASS_WITH_RISKS":
                required_caveats.extend(
                    f"[{item['finding_id']}] {item['summary']}"
                    for item in review.get("role_payload", {}).get("findings", [])
                )
            caveats = payload.get("caveats", [])
            missing = [item for item in required_caveats if item not in caveats]
            if missing:
                errors.append("Report omitted Review caveats: " + "; ".join(missing))
    return errors


def record_stage(run_dir: Path, stage_json: Path, stage_markdown: Path, validation_report: Path) -> dict[str, Any]:
    with RunLock(run_dir):
        state = load_state(run_dir, for_update=True)
        if state["status"] != "running" or not state["current_stage"]:
            raise ValueError("No running stage is available to record.")
        stage = _find_stage(state, state["current_stage"])
        resolved_paths = [resolve_within(run_dir, path) for path in (stage_json, stage_markdown, validation_report)]
        for resolved in resolved_paths:
            if not resolved.is_file():
                raise ValueError(f"Required stage artifact does not exist: {resolved}")
        stage_json, stage_markdown, validation_report = resolved_paths
        value = load_json(stage_json)
        if not isinstance(value, dict):
            raise ValueError("Stage JSON must contain an object.")
        errors = validate_stage(
            value,
            stage["role"],
            state["run_id"],
            stage["stage_id"],
            stage["attempt"],
            state["runtime"]["contract_version"],
        )
        if not errors:
            errors.extend(_validate_stage_context(run_dir, state, stage, value))
        if errors:
            _set_status(state, "failed")
            stage["status"] = "failed"
            completed_at = utc_now()
            stage["runtime"]["completed_at"] = completed_at
            stage["runtime"]["elapsed_ms"] = _elapsed_ms(stage["runtime"]["started_at"], completed_at)
            stage["runtime"]["failure_class"] = "stage_validation_failed"
            _commit(run_dir, state, "stage_validation_failed", {"stage_id": stage["stage_id"], "errors": errors})
            raise ValueError("Stage output is invalid:\n- " + "\n- ".join(errors))
        validation = load_json(validation_report)
        if not isinstance(validation, dict) or not validation.get("valid"):
            raise ValueError("Validation report must record a successful stage validation.")
        if validation.get("role") != stage["role"] or validation.get("stage_sha256") != sha256_json(value):
            raise ValueError("Validation report does not match the current stage artifact.")
        receipt = _verify_agent_receipt(run_dir, state, stage, stage_json, value)
        _set_status(state, "validating")
        artifact_hash = sha256_file(stage_json)
        artifact = {
            "artifact_id": new_id("stage_artifact"),
            "revision": stage["attempt"],
            "json_path": str(stage_json.resolve().relative_to(run_dir.resolve())).replace("\\", "/"),
            "markdown_path": str(stage_markdown.resolve().relative_to(run_dir.resolve())).replace("\\", "/"),
            "markdown_sha256": sha256_file(stage_markdown),
            "validation_path": str(validation_report.resolve().relative_to(run_dir.resolve())).replace("\\", "/"),
            "validation_sha256": sha256_file(validation_report),
            "sha256": artifact_hash,
            "stage_status": value["stage_status"],
            "receipt_path": stage["runtime"]["receipt_path"],
            "receipt_sha256": stage["runtime"]["receipt_sha256"],
            "raw_response_path": receipt["raw_response_path"],
            "raw_response_sha256": receipt["raw_response_sha256"],
        }
        stage["artifact"] = artifact
        completed_at = utc_now()
        stage["runtime"]["completed_at"] = completed_at
        stage["runtime"]["elapsed_ms"] = _elapsed_ms(stage["runtime"]["started_at"], completed_at)
        state["artifacts"].append({"stage_id": stage["stage_id"], **artifact})
        if stage["role"] == "growth-metrics":
            state["pending_metric_edit"] = None
        if value["stage_status"] == "BLOCKED":
            stage["runtime"]["failure_class"] = "agent_blocked"
            stage["status"] = "blocked"
            state["pending_approval"] = None
            state["current_stage"] = None
            state["pending_stage"] = stage["stage_id"]
            _set_status(state, "blocked")
        elif value["stage_status"] == "FAIL":
            stage["runtime"]["failure_class"] = "agent_reported_failure"
            stage["status"] = "failed"
            state["pending_approval"] = None
            state["current_stage"] = None
            state["pending_stage"] = stage["stage_id"]
            if stage["role"] == "growth-review":
                target_id = value["role_payload"]["rollback_stage"]
                target = _find_stage(state, target_id)
                if not target.get("artifact") or target["status"] != "approved":
                    raise ValueError("Review rollback target must be an approved stage artifact.")
                rollback_plan = {
                    "schema_version": CURRENT_CONTRACT_VERSION,
                    "run_id": state["run_id"],
                    "route_revision": state["route_revision"],
                    "rollback_revision": stage["attempt"],
                    "review_stage_id": stage["stage_id"],
                    "review_artifact_sha256": artifact_hash,
                    "target_stage_id": target_id,
                    "target_artifact_revision": target["artifact"]["revision"],
                    "target_artifact_sha256": target["artifact"]["sha256"],
                    "required_fixes": value["role_payload"]["required_fixes"],
                    "created_at": utc_now(),
                }
                validate_schema(rollback_plan, SCHEMA_DIR / "rollback-plan.schema.json")
                rollback_path = run_dir / "rollback-plan.json"
                atomic_write_json(rollback_path, rollback_plan)
                rollback_artifact = {
                    "artifact_id": new_id("rollback_plan"),
                    "kind": "rollback_plan",
                    "path": "rollback-plan.json",
                    "sha256": sha256_file(rollback_path),
                }
                for old in state["artifacts"]:
                    if old.get("kind") == "rollback_plan" and not old.get("superseded_by"):
                        old["superseded_by"] = rollback_artifact["artifact_id"]
                state["artifacts"].append(rollback_artifact)
                state["pending_approval"] = {
                    "approval_type": "rollback",
                    "subject_id": target_id,
                    "subject_revision": rollback_plan["rollback_revision"],
                    "subject_sha256": rollback_artifact["sha256"],
                }
            _set_status(state, "failed")
        else:
            stage["status"] = "awaiting_user_confirmation"
            state["pending_approval"] = {
                "approval_type": "stage",
                "subject_id": stage["stage_id"],
                "subject_revision": artifact["revision"],
                "subject_sha256": artifact_hash,
            }
            _set_status(state, "awaiting_user_confirmation")
        _commit(run_dir, state, "stage_recorded", {"stage_id": stage["stage_id"], "artifact": artifact})
        return state


def _approval_matches_pending(state: dict[str, Any], approval: dict[str, Any]) -> None:
    pending = state.get("pending_approval") if approval["approval_type"] != "query" else state.get("pending_query")
    if not pending:
        raise ValueError(f"No pending {approval['approval_type']} approval exists.")
    expected = {
        "approval_type": approval["approval_type"],
        "subject_id": approval["subject_id"],
        "subject_revision": approval["subject_revision"],
        "subject_sha256": approval["subject_sha256"],
    }
    for key, value in expected.items():
        if pending.get(key) != value:
            raise ValueError(f"Approval {key} does not match pending object.")


def approve(
    run_dir: Path,
    approval_type: str,
    subject_id: str,
    subject_revision: int,
    subject_sha256: str,
    action: str,
    user_text: str,
    idempotency_key: str,
    source_surface: str = "codex",
) -> dict[str, Any]:
    with RunLock(run_dir):
        state = load_state(run_dir, for_update=True)
        existing = next((item for item in read_jsonl(run_dir / APPROVALS_FILE) if item.get("idempotency_key") == idempotency_key), None)
        approval_already_appended = existing is not None
        if existing:
            fingerprint = (approval_type, subject_id, subject_revision, subject_sha256, action, user_text, source_surface)
            prior = tuple(existing.get(key) for key in ("approval_type", "subject_id", "subject_revision", "subject_sha256", "action", "user_text")) + (
                (existing.get("provenance") or {}).get("source_surface"),
            )
            if prior != fingerprint:
                raise ValueError("Idempotency key is already bound to a different approval request.")
            committed_event = next(
                (
                    event
                    for event in read_jsonl(run_dir / EVENTS_FILE)
                    if event.get("event_type") == "approval_recorded"
                    and event.get("payload", {}).get("approval_id") == existing.get("approval_id")
                ),
                None,
            )
            if committed_event:
                if state["last_event_id"] < committed_event["event_id"]:
                    raise ValueError("Approval was committed but run-state.json is stale; run recover before retrying.")
                return existing
        expected_action = APPROVAL_ACTIONS.get(approval_type)
        if expected_action is None or action != expected_action:
            raise ValueError(f"Approval type {approval_type} requires action {expected_action!r}.")
        if existing:
            approval = existing
        else:
            approval = {
                "schema_version": CURRENT_CONTRACT_VERSION,
                "approval_id": new_id("approval"),
                "approval_type": approval_type,
                "run_id": state["run_id"],
                "route_revision": state["route_revision"],
                "subject_id": subject_id,
                "subject_revision": subject_revision,
                "subject_sha256": subject_sha256,
                "action": action,
                "user_text": user_text,
                "created_at": utc_now(),
                "idempotency_key": idempotency_key,
                "carried_from_approval_id": None,
                "provenance": {
                    "source_surface": source_surface,
                    "source_message_id": None,
                    "source_message_sha256": sha256_bytes(user_text.encode("utf-8")),
                    "captured_at": utc_now(),
                    "trust_level": "self_asserted",
                    "capture_method": "root_cli",
                    "host_receipt": None,
                },
                "query": None,
            }
            if approval_type == "query":
                pending_query = state.get("pending_query") or {}
                approval["query"] = {
                    "query_sha256": pending_query.get("query_sha256"),
                    "source_sql_sha256": pending_query.get("source_sql_sha256"),
                    "sql_canonicalization": pending_query.get("sql_canonicalization"),
                    "metric_ids": pending_query.get("metric_ids", []),
                    "data_source_id": pending_query.get("data_source_id"),
                    "data_source_fingerprint": pending_query.get("data_source_fingerprint"),
                    "dialect": pending_query.get("dialect"),
                    "timeout_seconds": pending_query.get("timeout_seconds"),
                    "max_rows": pending_query.get("max_rows"),
                    "max_result_bytes": pending_query.get("max_result_bytes"),
                }
        validate_schema(approval, SCHEMA_DIR / "approval.schema.json")
        _approval_matches_pending(state, approval)

        if approval_type == "route":
            if state["status"] != "awaiting_route_confirmation":
                raise ValueError("Route is not awaiting confirmation.")
            route_path = run_dir / "route-plan.json"
            if not route_path.is_file() or sha256_file(route_path) != subject_sha256 or state.get("route_sha256") != subject_sha256:
                raise ValueError("Route file changed after it was proposed.")
            _load_verified_route(run_dir, state, require_executable=True)
            state["pending_approval"] = None
            state["pending_stage"] = _next_pending_stage(state)
            _set_status(state, "running" if state["pending_stage"] else "finalizing")
        elif approval_type == "stage":
            if state["status"] != "awaiting_user_confirmation":
                raise ValueError("Stage is not awaiting confirmation.")
            stage = _find_stage(state, subject_id)
            if stage["status"] != "awaiting_user_confirmation" or not stage["artifact"]:
                raise ValueError(f"Stage {subject_id} is not awaiting confirmation.")
            if stage["artifact"]["revision"] != subject_revision or stage["artifact"]["sha256"] != subject_sha256:
                raise ValueError("Stage approval does not match the current artifact.")
            _verified_stage_output(run_dir, stage)
            if stage["artifact"]["stage_status"] not in {"PASS", "PASS_WITH_RISKS"}:
                raise ValueError("Only PASS or PASS_WITH_RISKS stage artifacts can be approved.")
            stage["status"] = "approved"
            state["approved_artifacts"].append(
                {
                    "stage_id": subject_id,
                    "artifact_revision": subject_revision,
                    "artifact_sha256": subject_sha256,
                    "approval_id": approval["approval_id"],
                }
            )
            state["pending_approval"] = None
            state["current_stage"] = None
            state["pending_stage"] = _next_pending_stage(state)
            _set_status(state, "running" if state["pending_stage"] else "finalizing")
        elif approval_type == "query":
            if state["status"] != "awaiting_query_confirmation" or state["pending_query"].get("approved"):
                raise ValueError("Query is not awaiting confirmation.")
            state["pending_query"]["approved"] = True
            state["pending_query"]["approval_id"] = approval["approval_id"]
            _set_status(state, "running")
        elif approval_type == "rollback":
            if state["status"] != "failed":
                raise ValueError("Rollback is only available after a failed Review.")
            rollback_path = run_dir / "rollback-plan.json"
            if not rollback_path.is_file() or sha256_file(rollback_path) != subject_sha256:
                raise ValueError("Rollback plan changed after it was proposed.")
            rollback_plan = load_json(rollback_path)
            validate_schema(rollback_plan, SCHEMA_DIR / "rollback-plan.schema.json")
            if rollback_plan["target_stage_id"] != subject_id or rollback_plan["rollback_revision"] != subject_revision:
                raise ValueError("Rollback approval does not match the current plan.")
            review_stage = _find_stage(state, rollback_plan["review_stage_id"])
            if subject_id not in _descendants_reverse(state, review_stage["stage_id"]):
                raise ValueError("Rollback target is not an ancestor of the failed Review stage.")
            target = _find_stage(state, subject_id)
            if not target.get("artifact") or target["artifact"]["sha256"] != rollback_plan["target_artifact_sha256"]:
                raise ValueError("Rollback target artifact changed after Review.")
            affected = _descendants(state, subject_id)
            for affected_stage in state["stages"]:
                if affected_stage["stage_id"] == subject_id:
                    affected_stage["status"] = "revising"
                elif affected_stage["stage_id"] in affected and affected_stage["status"] not in {"pending", "skipped"}:
                    affected_stage["status"] = "stale"
            state["approved_artifacts"] = [item for item in state["approved_artifacts"] if item["stage_id"] not in affected]
            _invalidate_derived_artifacts(state, affected, "review_rollback")
            state["current_stage"] = None
            state["pending_approval"] = None
            state["pending_stage"] = subject_id
            state["pending_query"] = None
            state["pending_metric_edit"] = None
            _set_status(state, "revising")
        else:
            raise ValueError(f"Unsupported approval type: {approval_type}")
        validate_schema(state, SCHEMA_DIR / "run-state.schema.json")
        if not approval_already_appended:
            append_jsonl(run_dir / APPROVALS_FILE, approval)
        _commit(
            run_dir,
            state,
            "approval_recorded",
            {
                "approval_id": approval["approval_id"],
                "approval_type": approval_type,
                "approval_sha256": sha256_json(approval),
                "approval": deepcopy(approval),
            },
        )
        if approval_type == "route" and state["approved_artifacts"]:
            _commit(
                run_dir,
                state,
                "approval_carried_forward",
                {
                    "route_revision": state["route_revision"],
                    "approved_artifacts": deepcopy(state["approved_artifacts"]),
                },
            )
        return approval


def _find_execution_lease(state: dict[str, Any], lease_id: str) -> dict[str, Any]:
    for lease in state.get("execution_leases", []):
        if lease.get("lease_id") == lease_id:
            return lease
    raise ValueError(f"Unknown execution lease: {lease_id}")


def _lease_expired(lease: dict[str, Any]) -> bool:
    expires_at = datetime.fromisoformat(lease["expires_at"].replace("Z", "+00:00"))
    return datetime.now(timezone.utc) >= expires_at


def _execution_input_hashes(
    run_dir: Path,
    state: dict[str, Any],
    kind: str,
    input_artifact_ids: list[str],
) -> dict[str, str]:
    hashes: dict[str, str] = {}
    if state.get("route_sha256"):
        hashes["route"] = state["route_sha256"]
    for artifact_id in input_artifact_ids:
        artifact = artifact_by_id(state, artifact_id)
        relative = artifact.get("path") or artifact.get("json_path")
        path = resolve_within(run_dir, relative)
        if not path.is_file() or sha256_file(path) != artifact.get("sha256"):
            raise ValueError(f"Execution input artifact is missing or changed: {artifact_id}")
        hashes[f"artifact:{artifact_id}"] = artifact["sha256"]
    if kind == "query":
        pending = state.get("pending_query") or {}
        if not pending.get("approved") or not pending.get("approval_id"):
            raise ValueError("A query execution lease requires a current approved query.")
        hashes.update(
            {
                "query_subject": pending["subject_sha256"],
                "query_sql": pending["query_sha256"],
                "data_source": pending["data_source_fingerprint"],
                "query_approval": sha256_bytes(pending["approval_id"].encode("utf-8")),
            }
        )
    if not hashes:
        raise ValueError("An execution lease must bind at least one deterministic input hash.")
    return hashes


def begin_execution_lease(
    run_dir: Path,
    kind: str,
    subject_id: str,
    subject_revision: int,
    input_artifact_ids: list[str] | None = None,
    input_files: list[Path] | None = None,
    timeout_seconds: int = 3600,
) -> dict[str, Any]:
    if kind not in {"query", "charts", "lineage", "final_report"}:
        raise ValueError(f"Unsupported execution lease kind: {kind}")
    if timeout_seconds < 1 or timeout_seconds > 86400:
        raise ValueError("Execution lease timeout_seconds must be between 1 and 86400.")
    with RunLock(run_dir):
        state = load_state(run_dir, for_update=True)
        if state["status"] in {"completed", "stopped", "failed", "blocked"}:
            raise ValueError(f"Cannot begin execution while run status is {state['status']}.")
        if state["status"] == "revising" and kind != "lineage":
            raise ValueError(f"Only lineage may be rebuilt while run status is {state['status']}.")
        active = [
            item
            for item in state.get("execution_leases", [])
            if item["state"] in {"prepared", "executing", "publishing"}
        ]
        if active:
            raise ValueError(f"Execution lease {active[0]['lease_id']} is already active.")
        if kind == "query":
            pending = state.get("pending_query") or {}
            if (
                pending.get("subject_id") != subject_id
                or pending.get("subject_revision") != subject_revision
                or not pending.get("approved")
            ):
                raise ValueError("Execution lease does not match the current approved query revision.")
        input_hashes = _execution_input_hashes(run_dir, state, kind, input_artifact_ids or [])
        for input_file in input_files or []:
            path = resolve_within(run_dir, input_file)
            if not path.is_file():
                raise ValueError(f"Execution input file does not exist: {input_file}")
            relative = str(path.relative_to(run_dir.resolve())).replace("\\", "/")
            input_hashes[f"file:{relative}"] = sha256_file(path)
        now = datetime.now(timezone.utc)
        lease_id = new_id("lease")
        staging_path = f"data/.executions/{lease_id}"
        output_path = (
            f"data/queries/{subject_id}/revision-{subject_revision}/result"
            if kind == "query"
            else None
        )
        lease = {
            "schema_version": CURRENT_CONTRACT_VERSION,
            "lease_id": lease_id,
            "kind": kind,
            "subject_id": subject_id,
            "subject_revision": subject_revision,
            "route_revision": state["route_revision"],
            "input_hashes": input_hashes,
            "state": "prepared",
            "staging_path": staging_path,
            "output_path": output_path,
            "created_at": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "updated_at": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "expires_at": (now + timedelta(seconds=timeout_seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "abort_reason": None,
            "published_artifact_ids": [],
        }
        validate_schema(lease, SCHEMA_DIR / "execution-lease.schema.json")
        state["execution_leases"].append(lease)
        _commit(run_dir, state, "execution_lease_prepared", {"lease": deepcopy(lease)})
        staging = resolve_within(run_dir, staging_path)
        try:
            staging.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            lease["state"] = "aborted"
            lease["abort_reason"] = f"staging_create_failed:{type(exc).__name__}"
            lease["updated_at"] = utc_now()
            _commit(run_dir, state, "execution_lease_aborted", {"lease_id": lease_id, "reason": lease["abort_reason"]})
            raise
        lease["state"] = "executing"
        lease["updated_at"] = utc_now()
        _commit(run_dir, state, "execution_lease_executing", {"lease_id": lease_id})
        return deepcopy(lease)


def _validate_execution_lease_locked(run_dir: Path, state: dict[str, Any], lease: dict[str, Any]) -> None:
    if lease["state"] != "executing":
        raise ValueError(f"Execution lease is not publishable from state {lease['state']}.")
    if _lease_expired(lease):
        raise ValueError("Execution lease expired before publication.")
    allowed_statuses = {"running", "finalizing"}
    if lease["kind"] == "lineage":
        allowed_statuses.add("revising")
    if lease["kind"] == "charts":
        allowed_statuses.add("awaiting_user_confirmation")
    if state["status"] not in allowed_statuses:
        raise ValueError(f"Run status {state['status']} invalidated the execution lease.")
    if state["route_revision"] != lease["route_revision"]:
        raise ValueError("Route revision changed while the execution lease was active.")
    artifact_ids = [key.removeprefix("artifact:") for key in lease["input_hashes"] if key.startswith("artifact:")]
    current_hashes = _execution_input_hashes(run_dir, state, lease["kind"], artifact_ids)
    for key in lease["input_hashes"]:
        if key.startswith("file:"):
            relative = key.removeprefix("file:")
            path = resolve_within(run_dir, relative)
            if not path.is_file():
                raise ValueError(f"Execution input file is missing: {relative}")
            current_hashes[key] = sha256_file(path)
    if current_hashes != lease["input_hashes"]:
        raise ValueError("Execution inputs changed while the lease was active.")
    if lease["kind"] == "query":
        pending = state.get("pending_query") or {}
        if pending.get("subject_id") != lease["subject_id"] or pending.get("subject_revision") != lease["subject_revision"]:
            raise ValueError("The approved query changed while the execution lease was active.")


def _abort_execution_lease_locked(lease: dict[str, Any], reason: str) -> None:
    lease["state"] = "aborted"
    lease["abort_reason"] = reason
    lease["updated_at"] = utc_now()


def _abort_active_execution_leases(state: dict[str, Any], reason: str) -> list[str]:
    aborted: list[str] = []
    for lease in state.get("execution_leases", []):
        if lease["state"] in {"prepared", "executing", "publishing"}:
            _abort_execution_lease_locked(lease, reason)
            aborted.append(lease["lease_id"])
    return aborted


def abort_execution_lease(run_dir: Path, lease_id: str, reason: str) -> dict[str, Any]:
    with RunLock(run_dir):
        state = load_state(run_dir, for_update=True)
        lease = _find_execution_lease(state, lease_id)
        if lease["state"] in {"completed", "aborted"}:
            return deepcopy(lease)
        _abort_execution_lease_locked(lease, reason)
        _commit(run_dir, state, "execution_lease_aborted", {"lease_id": lease_id, "reason": reason})
        staging = resolve_within(run_dir, lease["staging_path"])
        if staging.is_dir():
            shutil.rmtree(staging)
        return deepcopy(lease)


def prepare_query(
    run_dir: Path,
    sql_file: Path,
    query_id: str,
    data_source_id: str,
    dialect: str,
    timeout_seconds: int,
    max_rows: int,
    max_result_bytes: int,
    data_source_fingerprint: str,
) -> dict[str, Any]:
    from sql_guard import SQL_CANONICALIZATION_VERSION, validate_and_rewrite

    if not QUERY_ID_RE.fullmatch(query_id):
        raise ValueError("query_id must contain 2-128 letters, digits, dots, underscores, or hyphens.")
    raw_sql = sql_file.read_text(encoding="utf-8")
    if not re.fullmatch(r"[A-Fa-f0-9]{64}", data_source_fingerprint):
        raise ValueError("data_source_fingerprint must be a 64-character SHA-256 value.")
    result = validate_and_rewrite(raw_sql, dialect=dialect, max_rows=max_rows)
    if not result.ok:
        raise ValueError("Query failed SQL safety validation:\n- " + "\n- ".join(result.errors))
    with RunLock(run_dir):
        state = load_state(run_dir, for_update=True)
        if state["status"] != "running":
            raise ValueError(f"Cannot prepare query while run status is {state['status']}.")
        if state.get("current_stage") or state.get("pending_query"):
            raise ValueError("Cannot prepare a query while another stage or query is active.")
        sql_stages = [item for item in state["stages"] if item["role"] == "growth-sql" and item["status"] == "approved" and item.get("artifact")]
        if not sql_stages:
            raise ValueError("A query requires an approved SQL stage.")
        sql_artifact = _verified_stage_output(run_dir, sql_stages[-1])
        proposal = next((item for item in sql_artifact["role_payload"]["queries"] if item["sql_id"] == query_id), None)
        if not proposal or not proposal.get("executable"):
            raise ValueError(f"Approved SQL artifact has no executable query named {query_id!r}.")
        if proposal["sql"].strip() != raw_sql.strip():
            raise ValueError("SQL input does not exactly match the approved SQL-stage proposal.")
        previous_revisions = [
            int(item.get("metadata", {}).get("query_revision", 0))
            for item in state["artifacts"]
            if item.get("kind") == "query_request" and item.get("metadata", {}).get("query_id") == query_id
        ]
        revision = max(previous_revisions, default=0) + 1
        query_dir = run_dir / "data" / "queries" / query_id / f"revision-{revision}"
        if query_dir.exists():
            raise ValueError("Immutable query revision directory already exists.")
        query_dir.mkdir(parents=True)
        final_sql_path = query_dir / "query.sql"
        atomic_write_text(final_sql_path, result.sql)
        sql_hash = sha256_file(final_sql_path)
        metric_ids = sorted({str(item) for item in proposal.get("metric_ids", []) if str(item).strip()})
        if not metric_ids:
            raise ValueError(f"Approved SQL query {query_id!r} must declare at least one metric_id.")
        fingerprint = {
            "query_id": query_id,
            "revision": revision,
            "sql_sha256": sql_hash,
            "source_sql_sha256": sha256_bytes(raw_sql.encode("utf-8")),
            "sql_canonicalization": SQL_CANONICALIZATION_VERSION,
            "metric_ids": metric_ids,
            "data_source_id": data_source_id,
            "data_source_fingerprint": data_source_fingerprint.lower(),
            "dialect": dialect,
            "timeout_seconds": timeout_seconds,
            "max_rows": max_rows,
            "max_result_bytes": max_result_bytes,
        }
        request = {
            "schema_version": CURRENT_CONTRACT_VERSION,
            **fingerprint,
            "sql_path": str(final_sql_path.relative_to(run_dir.resolve())).replace("\\", "/"),
            "subject_sha256": sha256_json(fingerprint),
            "warnings": result.warnings,
            "created_at": utc_now(),
        }
        validate_schema(request, SCHEMA_DIR / "query-request.schema.json")
        request_path = query_dir / "query-request.json"
        atomic_write_json(request_path, request)
        request_relative = str(request_path.relative_to(run_dir.resolve())).replace("\\", "/")
        sql_relative = str(final_sql_path.relative_to(run_dir.resolve())).replace("\\", "/")
        query_artifacts = [
            {
                "artifact_id": new_id("query_sql"),
                "kind": "query_sql",
                "path": sql_relative,
                "sha256": sql_hash,
                "metadata": {"query_id": query_id, "query_revision": revision},
            },
            {
                "artifact_id": new_id("query_request"),
                "kind": "query_request",
                "path": request_relative,
                "sha256": sha256_file(request_path),
                "metadata": {"query_id": query_id, "query_revision": revision},
            },
        ]
        state["artifacts"].extend(query_artifacts)
        state["pending_query"] = {
            "approval_type": "query",
            "subject_id": query_id,
            "subject_revision": revision,
            "subject_sha256": request["subject_sha256"],
            "query_sha256": sql_hash,
            "source_sql_sha256": fingerprint["source_sql_sha256"],
            "sql_canonicalization": SQL_CANONICALIZATION_VERSION,
            "metric_ids": metric_ids,
            "data_source_id": data_source_id,
            "data_source_fingerprint": data_source_fingerprint.lower(),
            "dialect": dialect,
            "timeout_seconds": timeout_seconds,
            "max_rows": max_rows,
            "max_result_bytes": max_result_bytes,
            "approved": False,
            "approval_id": None,
            "request_path": request_relative,
            "request_sha256": query_artifacts[1]["sha256"],
            "sql_path": sql_relative,
        }
        state["data_source"] = {
            "data_source_id": data_source_id,
            "data_source_fingerprint": data_source_fingerprint.lower(),
            "dialect": dialect,
        }
        _set_status(state, "awaiting_query_confirmation")
        _commit(run_dir, state, "query_prepared", {"query_request": request, "artifacts": query_artifacts})
        return request


def record_query_result(
    run_dir: Path,
    manifest_path: Path,
    result_path: Path,
    profile_path: Path,
    lease_id: str | None = None,
) -> dict[str, Any]:
    if not lease_id:
        raise ValueError("Query publication requires the execution lease created before database access.")
    with RunLock(run_dir):
        state = load_state(run_dir, for_update=True)
        lease = _find_execution_lease(state, lease_id)

        def abort(reason: str) -> None:
            _abort_execution_lease_locked(lease, reason)
            _commit(run_dir, state, "execution_lease_aborted", {"lease_id": lease_id, "reason": reason})

        try:
            _validate_execution_lease_locked(run_dir, state, lease)
            if lease["kind"] != "query":
                raise ValueError("Only a query execution lease can publish query results.")
            pending = state.get("pending_query")
            if not pending or not pending.get("approved"):
                raise ValueError("The current query has not been approved.")
            staging = resolve_within(run_dir, lease["staging_path"])
            manifest_path = resolve_within(run_dir, manifest_path)
            result_path = resolve_within(run_dir, result_path)
            profile_path = resolve_within(run_dir, profile_path)
            for path in (manifest_path, result_path, profile_path):
                if path.parent != staging:
                    raise ValueError("Query output files must be written directly inside the leased staging directory.")
                if not path.is_file():
                    raise ValueError(f"Missing staged query artifact: {path.name}")
            manifest = load_json(manifest_path)
            validate_schema(manifest, SCHEMA_DIR / "query-manifest.schema.json")
            profile = load_json(profile_path)
            validate_schema(profile, SCHEMA_DIR / "result-profile.schema.json")
            expected_manifest = {
                "query_id": pending.get("subject_id"),
                "query_revision": pending.get("subject_revision"),
                "metric_ids": pending.get("metric_ids"),
                "sql_sha256": pending.get("query_sha256"),
                "source_sql_sha256": pending.get("source_sql_sha256"),
                "sql_canonicalization": pending.get("sql_canonicalization"),
                "data_source_id": pending.get("data_source_id"),
                "data_source_fingerprint": pending.get("data_source_fingerprint"),
                "dialect": pending.get("dialect"),
                "timeout_seconds": pending.get("timeout_seconds"),
                "max_rows": pending.get("max_rows"),
                "max_result_bytes": pending.get("max_result_bytes"),
            }
            if any(manifest.get(key) != value for key, value in expected_manifest.items()):
                raise ValueError("Query manifest does not match the approved request.")
            output_relative = lease.get("output_path")
            if not output_relative:
                raise ValueError("Query execution lease has no immutable output path.")
            expected_result_path = f"{output_relative}/result.csv"
            expected_profile_path = f"{output_relative}/result-profile.json"
            if manifest["result_path"] != expected_result_path or manifest["profile_path"] != expected_profile_path:
                raise ValueError("Query manifest paths do not match the leased publication paths.")
            if manifest["result_bytes"] != result_path.stat().st_size:
                raise ValueError("Query result byte count does not match the manifest.")
            if sha256_file(result_path) != manifest["result_sha256"] or sha256_file(profile_path) != manifest["profile_sha256"]:
                raise ValueError("Query result or profile hash does not match the manifest.")
        except (OSError, ValueError) as exc:
            abort(f"publication_validation_failed:{type(exc).__name__}")
            raise

        final_dir = resolve_within(run_dir, lease["output_path"])
        if final_dir.exists():
            abort("immutable_output_already_exists")
            raise ValueError("Immutable query output directory already exists.")
        artifacts = []
        final_paths = {
            "query_manifest": final_dir / "query-manifest.json",
            "query_result": final_dir / "result.csv",
            "result_profile": final_dir / "result-profile.json",
        }
        for kind, staged in (
            ("query_manifest", manifest_path),
            ("query_result", result_path),
            ("result_profile", profile_path),
        ):
            artifacts.append(
                {
                    "artifact_id": new_id(kind),
                    "kind": kind,
                    "path": str(final_paths[kind].relative_to(run_dir.resolve())).replace("\\", "/"),
                    "sha256": sha256_file(staged),
                    "metadata": {
                        "query_id": lease["subject_id"],
                        "query_revision": lease["subject_revision"],
                        "lease_id": lease_id,
                    },
                }
            )
        lease["state"] = "publishing"
        lease["updated_at"] = utc_now()
        _commit(
            run_dir,
            state,
            "execution_lease_publishing",
            {"lease_id": lease_id, "output_path": lease["output_path"], "artifacts": deepcopy(artifacts)},
        )
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(resolve_within(run_dir, lease["staging_path"]), final_dir)
        except OSError:
            _abort_execution_lease_locked(lease, "atomic_publish_failed")
            _commit(run_dir, state, "execution_lease_aborted", {"lease_id": lease_id, "reason": lease["abort_reason"]})
            raise
        state["artifacts"].extend(artifacts)
        state["pending_query"] = None
        lease["state"] = "completed"
        lease["updated_at"] = utc_now()
        lease["published_artifact_ids"] = [item["artifact_id"] for item in artifacts]
        _set_status(state, "running")
        _commit(
            run_dir,
            state,
            "query_completed",
            {"query_id": manifest["query_id"], "lease_id": lease_id, "artifacts": artifacts},
        )
        return state


def register_artifacts(run_dir: Path, artifacts: list[dict[str, Any]], event_type: str = "artifacts_registered") -> list[dict[str, Any]]:
    with RunLock(run_dir):
        state = load_state(run_dir, for_update=True)
        if state["status"] == "completed":
            raise ValueError("Completed run artifacts are immutable.")
        records: list[dict[str, Any]] = []
        existing = {
            (item.get("kind"), item.get("path"), item.get("sha256"))
            for item in state["artifacts"]
        }
        registered_paths = {
            item.get("path"): item
            for item in state["artifacts"]
            if item.get("path")
        }
        for item in artifacts:
            kind = str(item.get("kind", "")).strip()
            if not ARTIFACT_KIND_RE.fullmatch(kind):
                raise ValueError("Registered artifact kind must use 2-64 lowercase letters, digits, underscores, or hyphens.")
            path = resolve_within(run_dir, item.get("path", ""))
            if not path.is_file():
                raise ValueError(f"Registered artifact does not exist: {path}")
            relative = str(path.relative_to(run_dir.resolve())).replace("\\", "/")
            digest = sha256_file(path)
            key = (kind, relative, digest)
            if key in existing:
                continue
            prior_at_path = registered_paths.get(relative)
            if prior_at_path is not None and prior_at_path.get("sha256") != digest:
                raise ValueError(
                    f"Registered artifact paths are immutable; publish a new versioned path instead of overwriting {relative}."
                )
            record = {
                "artifact_id": new_id(kind),
                "kind": kind,
                "path": relative,
                "sha256": digest,
            }
            if item.get("metadata") is not None:
                record["metadata"] = item["metadata"]
            state["artifacts"].append(record)
            records.append(record)
            existing.add(key)
            registered_paths[relative] = record
        if records:
            _commit(run_dir, state, event_type, {"artifacts": records})
        return records


def publish_execution_artifacts(
    run_dir: Path,
    lease_id: str,
    artifacts: list[dict[str, Any]],
    *,
    invalidate_kinds: set[str],
    event_type: str,
) -> list[dict[str, Any]]:
    with RunLock(run_dir):
        state = load_state(run_dir, for_update=True)
        lease = _find_execution_lease(state, lease_id)
        try:
            _validate_execution_lease_locked(run_dir, state, lease)
        except ValueError:
            _abort_execution_lease_locked(lease, "publication_inputs_changed")
            _commit(run_dir, state, "execution_lease_aborted", {"lease_id": lease_id, "reason": lease["abort_reason"]})
            raise
        records: list[dict[str, Any]] = []
        registered_paths = {
            item.get("path"): item
            for item in state["artifacts"]
            if item.get("path")
        }
        resolved_paths: list[Path] = []
        for item in artifacts:
            kind = str(item.get("kind", "")).strip()
            if not ARTIFACT_KIND_RE.fullmatch(kind):
                raise ValueError("Published artifact kind is invalid.")
            path = resolve_within(run_dir, item.get("path", ""))
            if not path.is_file():
                raise ValueError(f"Published artifact does not exist: {path}")
            relative = str(path.relative_to(run_dir.resolve())).replace("\\", "/")
            digest = sha256_file(path)
            prior_at_path = registered_paths.get(relative)
            if prior_at_path is not None and prior_at_path.get("sha256") != digest:
                raise ValueError(f"Published artifact path was previously registered with different content: {relative}")
            record = {
                "artifact_id": new_id(kind),
                "kind": kind,
                "path": relative,
                "sha256": digest,
                "metadata": {**(item.get("metadata") or {}), "lease_id": lease_id},
            }
            records.append(record)
            resolved_paths.append(path)
        if not records:
            raise ValueError("Execution publication requires at least one artifact.")
        common_parent = Path(os.path.commonpath([str(path.parent) for path in resolved_paths]))
        lease["output_path"] = str(common_parent.relative_to(run_dir.resolve())).replace("\\", "/")
        lease["state"] = "publishing"
        lease["updated_at"] = utc_now()
        _commit(
            run_dir,
            state,
            "execution_lease_publishing",
            {"lease_id": lease_id, "output_path": lease["output_path"], "artifacts": deepcopy(records)},
        )
        invalidation_id = new_id("invalidation")
        for old in state["artifacts"]:
            if old.get("kind") in invalidate_kinds and not old.get("superseded_by"):
                old["superseded_by"] = invalidation_id
        state["artifacts"].extend(records)
        lease["state"] = "completed"
        lease["updated_at"] = utc_now()
        lease["published_artifact_ids"] = [item["artifact_id"] for item in records]
        _commit(
            run_dir,
            state,
            event_type,
            {"lease_id": lease_id, "artifacts": records, "invalidated_by": invalidation_id},
        )
        return records


def invalidate_artifacts(run_dir: Path, kinds: set[str], reason: str) -> list[str]:
    with RunLock(run_dir):
        state = load_state(run_dir, for_update=True)
        if state["status"] == "completed":
            raise ValueError("Completed run artifacts are immutable.")
        invalidation_id = new_id("invalidation")
        invalidated: list[str] = []
        for artifact in state["artifacts"]:
            if artifact.get("kind") in kinds and not artifact.get("superseded_by"):
                artifact["superseded_by"] = invalidation_id
                invalidated.append(artifact["artifact_id"])
        if invalidated:
            _commit(
                run_dir,
                state,
                "artifacts_invalidated",
                {"reason": reason, "kinds": sorted(kinds), "artifact_ids": invalidated},
            )
        return invalidated


def ingest_artifact(run_dir: Path, source: Path, kind: str, name: str | None = None) -> dict[str, Any]:
    if load_state(run_dir, for_update=True)["status"] == "completed":
        raise ValueError("Completed run artifacts are immutable.")
    if not ARTIFACT_KIND_RE.fullmatch(kind):
        raise ValueError("Ingested artifact kind must use 2-64 lowercase letters, digits, underscores, or hyphens.")
    source = source.expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"Input artifact does not exist: {source}")
    file_name = name or source.name
    if Path(file_name).name != file_name or file_name in {".", ".."}:
        raise ValueError("Ingested artifact name must be a plain file name.")
    destination = resolve_within(run_dir, Path("data") / "inputs" / file_name)
    if destination.exists() and sha256_file(destination) != sha256_file(source):
        raise ValueError(f"An input named {file_name!r} already exists with different content.")
    if not destination.exists():
        atomic_copy_file(source, destination)
    records = register_artifacts(run_dir, [{"kind": kind, "path": destination}], event_type="input_ingested")
    if records:
        return records[0]
    state = load_state(run_dir, for_update=True)
    digest = sha256_file(destination)
    return next(item for item in state["artifacts"] if item.get("kind") == kind and item.get("sha256") == digest and item.get("path") == str(destination.relative_to(run_dir.resolve())).replace("\\", "/"))


def _descendants(state: dict[str, Any], stage_id: str) -> set[str]:
    affected = {stage_id}
    changed = True
    while changed:
        changed = False
        for stage in state["stages"]:
            if stage["stage_id"] not in affected and any(dependency in affected for dependency in stage["depends_on"]):
                affected.add(stage["stage_id"])
                changed = True
    return affected


def _descendants_reverse(state: dict[str, Any], stage_id: str) -> set[str]:
    by_id = {item["stage_id"]: item for item in state["stages"]}
    ancestors: set[str] = set()
    pending = list(by_id[stage_id]["depends_on"])
    while pending:
        current = pending.pop()
        if current in ancestors:
            continue
        ancestors.add(current)
        pending.extend(by_id[current]["depends_on"])
    return ancestors


def _invalidate_derived_artifacts(state: dict[str, Any], affected: set[str], reason: str) -> list[str]:
    affected_roles = {
        stage["role"]
        for stage in state["stages"]
        if stage["stage_id"] in affected and stage.get("artifact")
    }
    kinds: set[str] = {"run_summary"}
    if affected_roles & {"growth-business", "growth-metrics", "growth-sql"}:
        kinds.update({"query_manifest", "query_result", "result_profile"})
    if affected_roles & {"growth-business", "growth-metrics", "growth-sql", "growth-insight", "growth-visualization"}:
        kinds.update({"chart_specs", "chart_manifest", "chart_png", "chart_html"})
    if affected_roles & {"growth-metrics", "growth-sql", "growth-insight", "growth-visualization", "growth-report"}:
        kinds.update({"metric_lineage", "metric_lineage_latest"})
    invalidation_id = new_id("invalidation")
    invalidated: list[str] = []
    for artifact in state["artifacts"]:
        if artifact.get("kind") in kinds and not artifact.get("superseded_by"):
            artifact["superseded_by"] = invalidation_id
            invalidated.append(artifact["artifact_id"])
    return invalidated


def _revise_state(state: dict[str, Any], stage_id: str) -> tuple[dict[str, Any], set[str], list[str]]:
    _abort_active_execution_leases(state, "stage_revision")
    target = _find_stage(state, stage_id)
    affected = _descendants(state, stage_id)
    for stage in state["stages"]:
        if stage["stage_id"] == stage_id:
            stage["status"] = "revising"
        elif stage["stage_id"] in affected and stage["status"] not in {"pending", "skipped"}:
            stage["status"] = "stale"
    state["approved_artifacts"] = [item for item in state["approved_artifacts"] if item["stage_id"] not in affected]
    state["current_stage"] = None
    state["pending_stage"] = target["stage_id"]
    state["pending_approval"] = None
    state["pending_query"] = None
    invalidated = _invalidate_derived_artifacts(state, affected, "upstream_revision")
    _set_status(state, "revising")
    return target, affected, invalidated


def revise(run_dir: Path, stage_id: str, request_text: str) -> dict[str, Any]:
    with RunLock(run_dir):
        state = load_state(run_dir, for_update=True)
        if state["status"] not in {"awaiting_user_confirmation", "awaiting_query_confirmation", "running", "blocked", "failed"}:
            raise ValueError(f"Cannot revise a stage while run status is {state['status']}.")
        if state["status"] == "running" and (
            state.get("current_stage") or any(stage["status"] == "running" for stage in state["stages"])
        ):
            raise ValueError("Cannot revise while an Agent stage is running; stop or recover the active stage first.")
        if (state.get("pending_approval") or {}).get("approval_type") == "rollback":
            raise ValueError("The pending Review rollback must be approved before any revision starts.")
        state["pending_metric_edit"] = None
        _, affected, invalidated = _revise_state(state, stage_id)
        _commit(
            run_dir,
            state,
            "revision_requested",
            {
                "stage_id": stage_id,
                "request": request_text,
                "stale_stages": sorted(affected - {stage_id}),
                "invalidated_artifacts": invalidated,
            },
        )
        return state


def request_metric_edit(run_dir: Path, edit_file: Path) -> dict[str, Any]:
    edit = load_json(edit_file)
    if not isinstance(edit, dict):
        raise ValueError("Metric edit must contain a JSON object.")
    validate_schema(edit, SCHEMA_DIR / "metric-edit.schema.json")
    with RunLock(run_dir):
        state = load_state(run_dir, for_update=True)
        if state["status"] != "awaiting_user_confirmation":
            raise ValueError("Metric edits are accepted only while a Metrics artifact awaits confirmation.")
        stage = _find_stage(state, edit["stage_id"])
        if stage["role"] != "growth-metrics" or stage["status"] != "awaiting_user_confirmation":
            raise ValueError("Metric edit target must be the Metrics stage awaiting confirmation.")
        if edit["run_id"] != state["run_id"]:
            raise ValueError("Metric edit run_id does not match the current run.")
        current = _verified_stage_output(run_dir, stage)
        if edit["base_artifact_sha256"] != stage["artifact"]["sha256"]:
            raise ValueError("Metric edit is not based on the current Metrics artifact.")
        current_metrics = {
            metric["metric_id"]: metric
            for metric in current.get("role_payload", {}).get("metrics", [])
        }
        metric_id = edit["metric_id"]
        editable_fields = EDITABLE_METRIC_FIELDS
        if edit["operation"] == "batch":
            raise ValueError("Batch workbench edits must use 'metrics-workbench import'.")
        unknown_fields = set(edit["requested_changes"]) - editable_fields
        if unknown_fields:
            raise ValueError("Metric edit contains unsupported field(s): " + ", ".join(sorted(unknown_fields)))
        if edit["operation"] == "add":
            missing_fields = editable_fields - set(edit["requested_changes"])
            if missing_fields:
                raise ValueError("Metric addition is incomplete; missing field(s): " + ", ".join(sorted(missing_fields)))
        if edit["operation"] == "add" and metric_id in current_metrics:
            raise ValueError(f"Cannot add existing metric_id {metric_id}; use modify.")
        if edit["operation"] in {"modify", "delete"} and metric_id not in current_metrics:
            raise ValueError(f"Cannot {edit['operation']} unknown metric_id {metric_id}.")
        if edit["operation"] == "modify" and not any(
            current_metrics[metric_id].get(key) != value
            for key, value in edit["requested_changes"].items()
        ):
            raise ValueError("Metric modification does not change any current field.")
        edit_path = run_dir / "stages" / stage["stage_id"] / f"metric-edit-{edit['edit_id']}.json"
        if edit_path.exists():
            raise ValueError(f"Metric edit ID already exists: {edit['edit_id']}")
        atomic_write_json(edit_path, edit)
        edit_hash = sha256_file(edit_path)
        _, affected, invalidated = _revise_state(state, stage["stage_id"])
        edit_artifact = {
            "artifact_id": new_id("metric_edit"),
            "kind": "metric_edit",
            "path": str(edit_path.resolve().relative_to(run_dir.resolve())).replace("\\", "/"),
            "sha256": edit_hash,
        }
        state["artifacts"].append(edit_artifact)
        state["pending_metric_edit"] = {
            "edit_id": edit["edit_id"],
            "stage_id": stage["stage_id"],
            "operation": edit["operation"],
            "metric_id": metric_id,
            "base_artifact_sha256": edit["base_artifact_sha256"],
            "edit_path": edit_artifact["path"],
            "edit_sha256": edit_hash,
        }
        _commit(
            run_dir,
            state,
            "metric_edit_requested",
            {
                "edit": edit,
                "edit_sha256": edit_hash,
                "stale_stages": sorted(affected - {stage["stage_id"]}),
                "invalidated_artifacts": invalidated,
            },
        )
        return state


def import_metrics_workbench(run_dir: Path, workbench_file: Path) -> dict[str, Any]:
    workbench = _metric_workbench_payload(workbench_file.expanduser().resolve())
    with RunLock(run_dir):
        state = load_state(run_dir, for_update=True)
        if state["status"] != "awaiting_user_confirmation":
            raise ValueError("Metrics workbench imports are accepted only while a Metrics artifact awaits confirmation.")
        stage = next(
            (
                item
                for item in state["stages"]
                if item["role"] == "growth-metrics" and item["status"] == "awaiting_user_confirmation" and item.get("artifact")
            ),
            None,
        )
        if stage is None:
            raise ValueError("No Metrics stage is awaiting confirmation.")
        if workbench.get("run_id") != state["run_id"] or workbench.get("stage_id") != stage["stage_id"]:
            raise ValueError("Metrics workbench run_id or stage_id does not match the current Metrics stage.")
        if workbench.get("base_artifact_sha256") != stage["artifact"]["sha256"]:
            raise ValueError("Metrics workbench is not based on the current Metrics artifact.")
        current = _verified_stage_output(run_dir, stage)
        current_metrics = {item["metric_id"]: item for item in current["role_payload"]["metrics"]}
        normalized_metrics = deepcopy(workbench["metrics"])
        for metric in normalized_metrics:
            prior = current_metrics.get(metric["metric_id"])
            if prior is None:
                metric["version"] = 1
            else:
                changed = any(prior.get(field) != metric.get(field) for field in EDITABLE_METRIC_FIELDS)
                metric["version"] = prior["version"] + 1 if changed else prior["version"]
        operations = _metric_edit_operations(current_metrics, normalized_metrics)
        if not operations:
            raise ValueError("Metrics workbench does not change the current metric set.")
        for operation in operations:
            if operation["operation"] == "add":
                continue
            if operation["operation"] == "delete" and len(current_metrics) == 1:
                raise ValueError("Metrics workbench cannot delete the only remaining metric.")
        edit_id = f"workbench-{sha256_file(workbench_file)[:16]}"
        requested = {
            "metrics": normalized_metrics,
            "operations": deepcopy(operations),
        }
        edit = {
            "schema_version": CURRENT_CONTRACT_VERSION,
            "edit_id": edit_id,
            "run_id": state["run_id"],
            "stage_id": stage["stage_id"],
            "operation": "batch",
            "metric_id": "metrics-workbench",
            "requested_changes": requested,
            "user_text": f"Import metrics workbench {workbench_file.name}.",
            "base_artifact_sha256": stage["artifact"]["sha256"],
            "created_at": utc_now(),
        }
        validate_schema(edit, SCHEMA_DIR / "metric-edit.schema.json")
        edit_path = run_dir / "stages" / stage["stage_id"] / f"metric-edit-{edit_id}.json"
        if edit_path.exists():
            raise ValueError(f"Metric workbench edit ID already exists: {edit_id}")
        atomic_write_json(edit_path, edit)
        edit_hash = sha256_file(edit_path)
        _, affected, invalidated = _revise_state(state, stage["stage_id"])
        edit_artifact = {
            "artifact_id": new_id("metric_edit"),
            "kind": "metric_edit",
            "path": str(edit_path.resolve().relative_to(run_dir.resolve())).replace("\\", "/"),
            "sha256": edit_hash,
            "metadata": {"workbench_path": str(workbench_file.resolve()), "workbench_sha256": sha256_file(workbench_file)},
        }
        state["artifacts"].append(edit_artifact)
        state["pending_metric_edit"] = {
            "edit_id": edit_id,
            "stage_id": stage["stage_id"],
            "operation": "batch",
            "metric_id": "metrics-workbench",
            "base_artifact_sha256": stage["artifact"]["sha256"],
            "edit_path": edit_artifact["path"],
            "edit_sha256": edit_hash,
        }
        _commit(
            run_dir,
            state,
            "metric_workbench_imported",
            {
                "edit": edit,
                "edit_sha256": edit_hash,
                "workbench_sha256": sha256_file(workbench_file),
                "operations": operations,
                "stale_stages": sorted(affected - {stage["stage_id"]}),
                "invalidated_artifacts": invalidated,
            },
        )
        return state


def stop(run_dir: Path, reason: str) -> dict[str, Any]:
    with RunLock(run_dir):
        state = load_state(run_dir, for_update=True)
        if state["status"] == "completed":
            raise ValueError("Completed run cannot be stopped.")
        state["resume_status"] = state["status"]
        aborted_leases = _abort_active_execution_leases(state, "run_stopped")
        _set_status(state, "stopped")
        _commit(run_dir, state, "run_stopped", {"reason": reason, "aborted_leases": aborted_leases})
        return state


def _replay_event_chain(events: list[dict[str, Any]]) -> tuple[list[str], dict[str, Any] | None]:
    errors: list[str] = []
    previous_hash: str | None = None
    previous_event_hash: str | None = None
    previous_snapshot: dict[str, Any] | None = None
    for index, event in enumerate(events, start=1):
        if event.get("event_id") != index:
            errors.append("events.jsonl event_id sequence is not contiguous.")
            break
        if event.get("state_revision") != index:
            errors.append(f"Event {index} has an unexpected state_revision.")
        snapshot: dict[str, Any] | None = None
        try:
            snapshot = _event_state_after(event, previous_snapshot)
            validate_schema(snapshot, SCHEMA_DIR / "run-state.schema.json")
        except ValueError as exc:
            errors.append(f"Event {index} state reconstruction is invalid: {exc}")
        if snapshot is not None:
            snapshot_hash = sha256_json(snapshot)
            if event.get("state_sha256") != snapshot_hash:
                errors.append(f"Event {index} reconstructed state hash does not match.")
            if event.get("previous_state_sha256") != previous_hash:
                errors.append(f"Event {index} does not continue the state hash chain.")
            if snapshot.get("revision") != index or snapshot.get("last_event_id") != index:
                errors.append(f"Event {index} reconstructed state revision counters do not match.")
            if event.get("status") != snapshot.get("status"):
                errors.append(f"Event {index} status does not match its reconstructed state.")
        if event.get("schema_version") == CURRENT_CONTRACT_VERSION:
            if event.get("previous_event_sha256") != previous_event_hash:
                errors.append(f"Event {index} does not continue the event hash chain.")
            if event.get("event_sha256") != _event_content_sha256(event):
                errors.append(f"Event {index} content hash does not match.")
        elif event.get("schema_version") not in LEGACY_CONTRACT_VERSIONS:
            errors.append(f"Event {index} uses an unsupported contract version.")
        if snapshot is not None:
            previous_hash = sha256_json(snapshot)
            previous_snapshot = snapshot
        previous_event_hash = _event_chain_sha256(event)
    return errors, previous_snapshot


def _audit_event_chain(events: list[dict[str, Any]]) -> list[str]:
    errors, _ = _replay_event_chain(events)
    return errors


def audit_run(run_dir: Path) -> list[str]:
    errors: list[str] = []
    try:
        events = read_jsonl(run_dir / EVENTS_FILE)
    except ValueError as exc:
        return [str(exc)]
    errors.extend(_audit_event_chain(events))
    try:
        state = load_state(run_dir, authenticate=False)
    except Exception as exc:
        return errors + [str(exc)]
    if state["last_event_id"] != len(events):
        errors.append("run-state.json last_event_id does not match events.jsonl.")
    if events and sha256_json(state) != events[-1].get("state_sha256"):
        errors.append("run-state.json does not match the last committed event snapshot.")
    if state.get("route_sha256"):
        route_path = run_dir / "route-plan.json"
        if not route_path.is_file() or sha256_file(route_path) != state["route_sha256"]:
            errors.append("route-plan.json does not match the registered route hash.")
    for stage in state["stages"]:
        runtimes = list(stage.get("attempt_history", []))
        if stage.get("runtime"):
            runtimes.append(stage["runtime"])
            if stage["runtime"].get("attempt") != stage["attempt"]:
                errors.append(f"Stage {stage['stage_id']} current runtime attempt does not match its attempt counter.")
        attempt_ids = [item.get("attempt") for item in runtimes]
        if len(attempt_ids) != len(set(attempt_ids)):
            errors.append(f"Stage {stage['stage_id']} contains duplicate attempt runtime records.")
        if sorted(attempt_ids) != list(range(1, stage["attempt"] + 1)):
            errors.append(f"Stage {stage['stage_id']} attempt runtime history is incomplete.")
        for runtime in stage.get("attempt_history", []):
            if runtime.get("completed_at") is None:
                errors.append(f"Stage {stage['stage_id']} has an unfinished historical attempt.")
        for attempt in range(1, stage["attempt"] + 1):
            attempt_dir = run_dir / "stages" / stage["stage_id"] / f"attempt-{attempt}"
            if not attempt_dir.is_dir():
                errors.append(f"Stage {stage['stage_id']} is missing attempt directory {attempt}.")
        if stage.get("artifact") and stage["status"] in {"approved", "completed", "awaiting_user_confirmation"}:
            try:
                value = _verified_stage_output(run_dir, stage)
                context_errors = _validate_stage_context(run_dir, state, stage, value)
                errors.extend(f"Stage {stage['stage_id']} audit: {error}" for error in context_errors)
                if stage["artifact"].get("receipt_path"):
                    stage_path = resolve_within(run_dir, stage["artifact"]["json_path"])
                    _verify_agent_receipt(run_dir, state, stage, stage_path, value)
            except (OSError, ValueError) as exc:
                errors.append(f"Stage {stage['stage_id']} audit failed: {exc}")
    artifact_ids = [item.get("artifact_id") for item in state["artifacts"] if item.get("artifact_id")]
    if len(artifact_ids) != len(set(artifact_ids)):
        errors.append("Run contains duplicate artifact_id values.")
    for artifact in state["artifacts"]:
        try:
            relative = artifact.get("json_path") or artifact.get("path")
            if not relative:
                raise ValueError("Artifact has neither json_path nor path.")
            path = resolve_within(run_dir, relative)
            if not path.is_file():
                errors.append(f"Missing artifact: {relative}")
            elif sha256_file(path) != artifact["sha256"]:
                errors.append(f"Artifact hash mismatch: {relative}")
            for path_key, hash_key in (
                ("markdown_path", "markdown_sha256"),
                ("validation_path", "validation_sha256"),
                ("receipt_path", "receipt_sha256"),
                ("raw_response_path", "raw_response_sha256"),
            ):
                if path_key in artifact:
                    companion = resolve_within(run_dir, artifact[path_key])
                    if not companion.is_file():
                        errors.append(f"Missing artifact: {artifact[path_key]}")
                    elif sha256_file(companion) != artifact[hash_key]:
                        errors.append(f"Artifact hash mismatch: {artifact[path_key]}")
        except (KeyError, ValueError) as exc:
            errors.append(str(exc))
    lease_ids: set[str] = set()
    artifacts_by_id = {item.get("artifact_id"): item for item in state["artifacts"]}
    for lease in state.get("execution_leases", []):
        try:
            validate_schema(lease, SCHEMA_DIR / "execution-lease.schema.json")
        except ValueError as exc:
            errors.append(str(exc))
        lease_id = lease.get("lease_id")
        if lease_id in lease_ids:
            errors.append(f"Duplicate execution lease ID: {lease_id}")
        lease_ids.add(lease_id)
        if lease.get("state") == "completed":
            if not lease.get("published_artifact_ids"):
                errors.append(f"Completed execution lease {lease_id} has no published artifacts.")
            for artifact_id in lease.get("published_artifact_ids", []):
                artifact = artifacts_by_id.get(artifact_id)
                if artifact is None:
                    errors.append(f"Execution lease {lease_id} references missing artifact {artifact_id}.")
                elif artifact.get("metadata", {}).get("lease_id") != lease_id:
                    errors.append(f"Execution lease {lease_id} artifact {artifact_id} is not bound back to the lease.")
        if lease.get("state") in {"prepared", "executing"}:
            try:
                staging = resolve_within(run_dir, lease["staging_path"])
                if not staging.is_dir():
                    errors.append(f"Active execution lease {lease_id} is missing its staging directory.")
            except (KeyError, ValueError) as exc:
                errors.append(str(exc))
        if lease.get("state") == "publishing":
            staging = resolve_within(run_dir, lease["staging_path"])
            output = resolve_within(run_dir, lease["output_path"]) if lease.get("output_path") else None
            if not staging.is_dir() and (output is None or not output.is_dir()):
                errors.append(f"Publishing execution lease {lease_id} has neither staged nor published files.")
    try:
        approvals = read_jsonl(run_dir / APPROVALS_FILE)
    except ValueError as exc:
        approvals = []
        errors.append(str(exc))
    idempotency_keys: set[str] = set()
    approval_ids: set[str] = set()
    recorded: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("event_type") != "approval_recorded":
            continue
        payload = event.get("payload", {})
        approval_id = payload.get("approval_id")
        event_approval = payload.get("approval")
        if not isinstance(approval_id, str) or not isinstance(event_approval, dict):
            errors.append(f"Approval event {event.get('event_id')} is missing its immutable approval payload.")
            continue
        if approval_id in recorded:
            errors.append(f"Duplicate approval event for {approval_id}.")
        if event_approval.get("approval_id") != approval_id or payload.get("approval_sha256") != sha256_json(event_approval):
            errors.append(f"Approval event {event.get('event_id')} payload hash or identity does not match.")
        recorded[approval_id] = event_approval
    approvals_by_id: dict[str, dict[str, Any]] = {}
    for approval in approvals:
        try:
            validate_schema(approval, SCHEMA_DIR / "approval.schema.json")
        except ValueError as exc:
            errors.append(str(exc))
        if approval.get("schema_version") == CURRENT_CONTRACT_VERSION:
            provenance = approval.get("provenance") or {}
            expected_message_hash = sha256_bytes(str(approval.get("user_text", "")).encode("utf-8"))
            if provenance.get("source_message_sha256") != expected_message_hash:
                errors.append(f"Approval {approval.get('approval_id')} user-text provenance hash does not match.")
            if provenance.get("trust_level") == "host_signed" and not provenance.get("host_receipt"):
                errors.append(f"Approval {approval.get('approval_id')} claims host_signed without a host receipt.")
        key = approval.get("idempotency_key")
        if key in idempotency_keys:
            errors.append(f"Duplicate approval idempotency key: {key}")
        idempotency_keys.add(key)
        approval_id = approval.get("approval_id")
        approval_ids.add(approval_id)
        if approval_id in approvals_by_id:
            errors.append(f"Duplicate approval ID: {approval_id}")
        approvals_by_id[approval_id] = approval
        if approval_id not in recorded:
            errors.append(f"Approval {approval_id} has no committed approval event.")
        elif sha256_json(approval) != sha256_json(recorded[approval_id]):
            errors.append(f"Approval {approval_id} differs from its committed event payload.")
    for approval_id in sorted(set(recorded) - set(approvals_by_id)):
        errors.append(f"Committed approval event {approval_id} has no approval record.")
    for item in state["approved_artifacts"]:
        try:
            stage = _find_stage(state, item["stage_id"])
            if not stage.get("artifact") or stage["artifact"]["sha256"] != item["artifact_sha256"]:
                errors.append(f"Approved artifact no longer matches stage {item['stage_id']}.")
            if item["approval_id"] not in approval_ids:
                errors.append(f"Approved stage {item['stage_id']} references a missing approval.")
        except ValueError as exc:
            errors.append(str(exc))
    pending_edit = state.get("pending_metric_edit")
    if pending_edit:
        try:
            edit_path = resolve_within(run_dir, pending_edit["edit_path"])
            if not edit_path.is_file() or sha256_file(edit_path) != pending_edit["edit_sha256"]:
                errors.append("Pending metric edit file is missing or changed.")
        except (KeyError, ValueError) as exc:
            errors.append(str(exc))
    if state["status"] == "completed":
        active_artifacts = [item for item in state["artifacts"] if not item.get("superseded_by")]
        active_kinds = {item.get("kind") for item in active_artifacts}
        summaries = [item for item in active_artifacts if item.get("kind") == "run_summary"]
        final_reports = [item for item in active_artifacts if item.get("kind") == "final_report"]
        final_manifests = [item for item in active_artifacts if item.get("kind") == "final_report_manifest"]
        completion_kind = state.get("completion_kind")
        if completion_kind not in {"report", "review_terminal"}:
            errors.append("Completed run has no valid completion_kind.")
        elif completion_kind == "report" and (len(final_reports) != 1 or len(final_manifests) != 1):
            errors.append("Report-completed run is missing its unique final report or manifest artifact.")
        elif completion_kind == "review_terminal" and (final_reports or final_manifests):
            errors.append("Review-terminal run must not contain final report artifacts.")
        if len(summaries) != 1:
            errors.append("Completed run is missing an active run_summary artifact.")
        else:
            try:
                summary = load_json(resolve_within(run_dir, summaries[0]["path"]))
                if (
                    summary.get("run_id") != state["run_id"]
                    or summary.get("status") != "completed"
                    or summary.get("state_revision") != state["revision"]
                    or summary.get("completion_kind") != completion_kind
                    or summary.get("final_report_artifact_id") != (final_reports[0].get("artifact_id") if completion_kind == "report" and final_reports else None)
                ):
                    errors.append("Completed run summary does not match the final state identity or revision.")
            except (AttributeError, OSError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"Completed run summary is invalid: {exc}")
        if any(item["role"] == "growth-metrics" and item["status"] in {"approved", "completed"} for item in state["stages"]):
            if "metric_lineage_latest" not in active_kinds:
                errors.append("Completed run with Metrics is missing an active metric_lineage_latest artifact.")
    return errors


def recover_state(run_dir: Path) -> dict[str, Any]:
    with RunLock(run_dir):
        events = read_jsonl(run_dir / EVENTS_FILE, tolerate_truncated_tail=True)
        if not events:
            raise ValueError("No committed event is available for recovery.")
        errors, recovered = _replay_event_chain(events)
        if errors:
            raise ValueError("Event log cannot be recovered:\n- " + "\n- ".join(errors))
        if recovered is None:
            raise ValueError("Event log has no recoverable state.")
        recovered_approvals: list[dict[str, Any]] = []
        for event in events:
            if event.get("event_type") != "approval_recorded":
                continue
            approval = event.get("payload", {}).get("approval")
            if not isinstance(approval, dict):
                raise ValueError(f"Approval event {event.get('event_id')} cannot rebuild approvals.jsonl.")
            validate_schema(approval, SCHEMA_DIR / "approval.schema.json")
            recovered_approvals.append(approval)
        atomic_write_jsonl(run_dir / EVENTS_FILE, events)
        if recovered_approvals or (run_dir / APPROVALS_FILE).exists():
            atomic_write_jsonl(run_dir / APPROVALS_FILE, recovered_approvals)
        recovered = deepcopy(recovered)
        atomic_write_json(_state_path(run_dir), recovered)
        errors = audit_run(run_dir)
        if errors:
            raise ValueError("State snapshot was restored, but the run still has consistency errors:\n- " + "\n- ".join(errors))
        _commit(
            run_dir,
            recovered,
            "state_recovered",
            {
                "source_event_id": events[-1]["event_id"],
                "source_state_sha256": events[-1]["state_sha256"],
                "rebuilt_approval_count": len(recovered_approvals),
            },
        )
        return recovered


def recover_execution_leases(run_dir: Path) -> dict[str, Any]:
    with RunLock(run_dir):
        state = load_state(run_dir, for_update=True)
        events = read_jsonl(run_dir / EVENTS_FILE)
        recovered: list[str] = []
        aborted: list[str] = []
        cleanup: list[Path] = []
        existing_ids = {item.get("artifact_id") for item in state["artifacts"]}
        for lease in state.get("execution_leases", []):
            if lease["state"] in {"completed", "aborted"}:
                continue
            staging = resolve_within(run_dir, lease["staging_path"])
            output = resolve_within(run_dir, lease["output_path"]) if lease.get("output_path") else None
            if lease["state"] != "publishing" or output is None or not output.is_dir():
                _abort_execution_lease_locked(lease, "recovered_interrupted_execution")
                aborted.append(lease["lease_id"])
                if staging.is_dir():
                    cleanup.append(staging)
                continue
            publishing_event = next(
                (
                    event
                    for event in reversed(events)
                    if event.get("event_type") == "execution_lease_publishing"
                    and event.get("payload", {}).get("lease_id") == lease["lease_id"]
                ),
                None,
            )
            descriptors = publishing_event.get("payload", {}).get("artifacts", []) if publishing_event else []
            valid_descriptors = bool(descriptors)
            for descriptor in descriptors:
                try:
                    path = resolve_within(run_dir, descriptor["path"])
                    if not path.is_file() or sha256_file(path) != descriptor["sha256"]:
                        valid_descriptors = False
                        break
                except (KeyError, ValueError):
                    valid_descriptors = False
                    break
            if not valid_descriptors:
                _abort_execution_lease_locked(lease, "recovered_publication_is_incomplete")
                aborted.append(lease["lease_id"])
                continue
            for descriptor in descriptors:
                if descriptor["artifact_id"] not in existing_ids:
                    state["artifacts"].append(descriptor)
                    existing_ids.add(descriptor["artifact_id"])
            lease["state"] = "completed"
            lease["updated_at"] = utc_now()
            lease["published_artifact_ids"] = [item["artifact_id"] for item in descriptors]
            if lease["kind"] == "query":
                pending = state.get("pending_query") or {}
                if pending.get("subject_id") == lease["subject_id"] and pending.get("subject_revision") == lease["subject_revision"]:
                    state["pending_query"] = None
                _set_status(state, "running")
            recovered.append(lease["lease_id"])
        if recovered or aborted:
            _commit(
                run_dir,
                state,
                "execution_leases_recovered",
                {"completed": recovered, "aborted": aborted},
            )
        for staging in cleanup:
            shutil.rmtree(staging, ignore_errors=True)
        return state


def build_run_summary(run_dir: Path, output: Path | None = None) -> dict[str, Any]:
    state = load_state(run_dir, for_update=output is not None)
    if output and state["status"] == "completed":
        raise ValueError("Completed run artifacts are immutable.")
    approvals = read_jsonl(run_dir / APPROVALS_FILE)
    attempts: list[dict[str, Any]] = []
    total_input = 0
    total_output = 0
    token_records = 0
    for stage in state["stages"]:
        runtimes = list(stage.get("attempt_history", []))
        if stage.get("runtime"):
            runtimes.append(stage["runtime"])
        for runtime in runtimes:
            usage = runtime.get("token_usage")
            if usage and usage.get("input_tokens") is not None and usage.get("output_tokens") is not None:
                total_input += usage["input_tokens"]
                total_output += usage["output_tokens"]
                token_records += 1
            attempts.append(
                {
                    "stage_id": stage["stage_id"],
                    "role": stage["role"],
                    **runtime,
                }
            )
    current_artifacts = [item for item in state["artifacts"] if not item.get("superseded_by")]
    failures = [
        {
            "stage_id": item["stage_id"],
            "role": item["role"],
            "attempt": item["attempt"],
            "failure_class": item["failure_class"],
        }
        for item in attempts
        if item.get("failure_class")
    ]
    action = {
        "awaiting_route_confirmation": "Confirm or revise the current route.",
        "awaiting_user_confirmation": "Confirm or revise the current stage artifact.",
        "awaiting_query_confirmation": "Approve or reject the exact prepared query.",
        "finalizing": "Build final lineage and finalize the run.",
        "blocked": "Provide the missing input or revise the blocked stage.",
        "failed": "Inspect the failure and revise the responsible stage.",
        "stopped": "Resume or leave the run stopped.",
        "completed": "No workflow action is required.",
    }.get(state["status"], f"Continue with stage {state.get('pending_stage') or state.get('current_stage')}.")
    summary = {
        "schema_version": CURRENT_CONTRACT_VERSION,
        "generated_at": utc_now(),
        "run_id": state["run_id"],
        "state_revision": state["revision"],
        "state_sha256": sha256_json(state),
        "status": state["status"],
        "completion_kind": state.get("completion_kind"),
        "route_id": state["route_id"],
        "route_revision": state["route_revision"],
        "runtime": state["runtime"],
        "stages": [
            {
                "stage_id": item["stage_id"],
                "role": item["role"],
                "status": item["status"],
                "attempts": item["attempt"],
                "artifact_sha256": item.get("artifact", {}).get("sha256") if item.get("artifact") else None,
            }
            for item in state["stages"]
        ],
        "attempts": attempts,
        "approvals": {
            approval_type: sum(1 for item in approvals if item.get("approval_type") == approval_type)
            for approval_type in APPROVAL_ACTIONS
        },
        "artifacts": current_artifacts,
        "failures": failures,
        "token_usage": {
            "available_attempts": token_records,
            "unavailable_attempts": len(attempts) - token_records,
            "input_tokens": total_input if token_records else None,
            "output_tokens": total_output if token_records else None,
            "total_tokens": total_input + total_output if token_records else None,
        },
        "recommended_action": action,
    }
    if output:
        output = resolve_within(run_dir, output)
        atomic_write_json(output, summary)
        register_artifacts(run_dir, [{"kind": "run_summary", "path": output}], event_type="run_summary_registered")
    return summary


def publish_final_report(run_dir: Path) -> dict[str, Any]:
    state = load_state(run_dir, for_update=True)
    if state["status"] != "finalizing":
        raise ValueError(f"Final report cannot be published from status {state['status']}.")
    reports = [
        stage
        for stage in state["stages"]
        if stage["role"] == "growth-report" and stage["status"] == "approved" and stage.get("artifact")
    ]
    if len(reports) != 1:
        raise ValueError("Final report publication requires exactly one user-approved Report stage.")
    report_stage = reports[0]
    report_artifact = report_stage["artifact"]
    existing = [
        item
        for item in state["artifacts"]
        if item.get("kind") == "final_report"
        and item.get("metadata", {}).get("report_artifact_id") == report_artifact["artifact_id"]
        and not item.get("superseded_by")
    ]
    if existing:
        path = resolve_within(run_dir, existing[-1]["path"])
        if path.is_file() and sha256_file(path) == existing[-1]["sha256"]:
            return existing[-1]
        raise ValueError("Existing final report artifact is missing or changed.")

    reviews = [
        stage
        for stage in state["stages"]
        if stage["role"] == "growth-review" and stage["status"] == "approved" and stage.get("artifact")
    ]
    if not reviews:
        raise ValueError("Final report publication requires an approved passing Review stage.")
    review_stage = reviews[-1]
    review_output = _verified_stage_output(run_dir, review_stage)
    review_decision = review_output.get("role_payload", {}).get("decision")
    if review_decision not in {"PASS", "PASS_WITH_RISKS"}:
        raise ValueError("Final report publication requires a passing Review decision.")
    active = [item for item in state["artifacts"] if not item.get("superseded_by")]
    lineage = next((item for item in reversed(active) if item.get("kind") == "metric_lineage_latest"), None)
    chart_manifest = next((item for item in reversed(active) if item.get("kind") == "chart_manifest"), None)
    input_artifact_ids = [report_artifact["artifact_id"], review_stage["artifact"]["artifact_id"]]
    if lineage:
        input_artifact_ids.append(lineage["artifact_id"])
    if chart_manifest:
        input_artifact_ids.append(chart_manifest["artifact_id"])

    lease = begin_execution_lease(
        run_dir,
        "final_report",
        report_stage["stage_id"],
        report_stage["attempt"],
        input_artifact_ids=input_artifact_ids,
        timeout_seconds=300,
    )
    staging = resolve_within(run_dir, lease["staging_path"])
    source_markdown = resolve_within(run_dir, report_artifact["markdown_path"])
    staged_report = staging / "final-report.md"
    try:
        atomic_copy_file(source_markdown, staged_report)
        if staged_report.stat().st_size == 0:
            raise ValueError("Final report content is empty.")
        report_digest = sha256_file(staged_report)
        output_relative = f"final/reports/report-r{report_stage['attempt']}-{report_digest[:12]}"
        final_report_relative = f"{output_relative}/final-report.md"
        manifest_relative = f"{output_relative}/final-report-manifest.json"
        manifest = {
            "schema_version": CURRENT_CONTRACT_VERSION,
            "run_id": state["run_id"],
            "lease_id": lease["lease_id"],
            "report_stage_id": report_stage["stage_id"],
            "report_artifact_id": report_artifact["artifact_id"],
            "report_stage_sha256": report_artifact["sha256"],
            "review_artifact_id": review_stage["artifact"]["artifact_id"],
            "review_decision": review_decision,
            "lineage_artifact_id": lineage["artifact_id"] if lineage else None,
            "chart_manifest_artifact_id": chart_manifest["artifact_id"] if chart_manifest else None,
            "final_report_path": final_report_relative,
            "final_report_sha256": report_digest,
            "final_report_bytes": staged_report.stat().st_size,
            "created_at": utc_now(),
        }
        validate_schema(manifest, SCHEMA_DIR / "final-report-manifest.schema.json")
        staged_manifest = staging / "final-report-manifest.json"
        atomic_write_json(staged_manifest, manifest)
    except Exception:
        abort_execution_lease(run_dir, lease["lease_id"], "final_report_render_failed")
        raise

    with RunLock(run_dir):
        state = load_state(run_dir, for_update=True)
        current_lease = _find_execution_lease(state, lease["lease_id"])
        try:
            _validate_execution_lease_locked(run_dir, state, current_lease)
        except ValueError:
            _abort_execution_lease_locked(current_lease, "final_report_inputs_changed")
            _commit(
                run_dir,
                state,
                "execution_lease_aborted",
                {"lease_id": current_lease["lease_id"], "reason": current_lease["abort_reason"]},
            )
            raise
        current_lease["output_path"] = output_relative
        current_lease["state"] = "publishing"
        current_lease["updated_at"] = utc_now()
        final_report_record = {
            "artifact_id": new_id("final_report"),
            "kind": "final_report",
            "path": final_report_relative,
            "sha256": report_digest,
            "metadata": {
                "lease_id": current_lease["lease_id"],
                "report_stage_id": report_stage["stage_id"],
                "report_artifact_id": report_artifact["artifact_id"],
                "report_stage_sha256": report_artifact["sha256"],
                "review_artifact_id": review_stage["artifact"]["artifact_id"],
                "review_decision": review_decision,
                "lineage_artifact_id": lineage["artifact_id"] if lineage else None,
                "chart_manifest_artifact_id": chart_manifest["artifact_id"] if chart_manifest else None,
            },
        }
        manifest_record = {
            "artifact_id": new_id("final_report_manifest"),
            "kind": "final_report_manifest",
            "path": manifest_relative,
            "sha256": sha256_file(staged_manifest),
            "metadata": {"lease_id": current_lease["lease_id"], "final_report_artifact_id": final_report_record["artifact_id"]},
        }
        _commit(
            run_dir,
            state,
            "execution_lease_publishing",
            {
                "lease_id": current_lease["lease_id"],
                "output_path": output_relative,
                "artifacts": [deepcopy(final_report_record), deepcopy(manifest_record)],
            },
        )
        output_dir = resolve_within(run_dir, output_relative)
        if output_dir.exists():
            _abort_execution_lease_locked(current_lease, "immutable_output_already_exists")
            _commit(run_dir, state, "execution_lease_aborted", {"lease_id": current_lease["lease_id"], "reason": current_lease["abort_reason"]})
            raise ValueError("Immutable final report directory already exists.")
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(resolve_within(run_dir, current_lease["staging_path"]), output_dir)
        state["artifacts"].extend([final_report_record, manifest_record])
        current_lease["state"] = "completed"
        current_lease["updated_at"] = utc_now()
        current_lease["published_artifact_ids"] = [final_report_record["artifact_id"], manifest_record["artifact_id"]]
        _commit(
            run_dir,
            state,
            "final_report_published",
            {"lease_id": current_lease["lease_id"], "artifacts": [final_report_record, manifest_record]},
        )
        return final_report_record


def finalize_run(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    with RunLock(run_dir):
        state = load_state(run_dir, for_update=True)
        if state["status"] != "finalizing":
            raise ValueError(f"Run cannot finalize from status {state['status']}.")
        consistency_errors = audit_run(run_dir)
        if consistency_errors:
            raise ValueError("Run cannot finalize with consistency errors:\n- " + "\n- ".join(consistency_errors))
        reports = [
            stage
            for stage in state["stages"]
            if stage["role"] == "growth-report" and stage["status"] == "approved" and stage.get("artifact")
        ]
        has_report_route = _route_has_role(state, "growth-report")
        if has_report_route and len(reports) != 1:
            raise ValueError("Finalization requires exactly one user-approved Report stage for a Report route.")
        if not has_report_route and reports:
            raise ValueError("A Review-terminal route cannot contain an approved Report stage.")
        if reports:
            _verified_stage_output(run_dir, reports[0])
        reviews = _approved_review_stages(state)
        if not reviews:
            raise ValueError("Finalization requires an approved Review result of PASS or PASS_WITH_RISKS.")
        if len(reviews) != 1:
            raise ValueError("Finalization requires exactly one approved Review result.")
        review_stage = reviews[0]
        _verified_stage_output(run_dir, review_stage)
        if any(stage["status"] not in {"approved", "skipped"} for stage in state["stages"]):
            raise ValueError("Finalization requires every routed stage to be approved or skipped.")
        active_artifacts = [item for item in state["artifacts"] if not item.get("superseded_by")]
        active_kinds = {item.get("kind") for item in active_artifacts}
        if any(stage["role"] == "growth-metrics" and stage["status"] in {"approved", "completed"} for stage in state["stages"]):
            if "metric_lineage_latest" not in active_kinds:
                raise ValueError("Finalization requires an active metric lineage artifact.")
            if reports:
                report_hash = reports[0]["artifact"]["sha256"]
                report_index = next(
                    index
                    for index, artifact in enumerate(state["artifacts"])
                    if artifact.get("stage_id") == reports[0]["stage_id"] and artifact.get("sha256") == report_hash
                )
                lineage_indices = [
                    index
                    for index, artifact in enumerate(state["artifacts"])
                    if artifact.get("kind") == "metric_lineage_latest" and not artifact.get("superseded_by")
                ]
                if not lineage_indices or lineage_indices[-1] <= report_index:
                    raise ValueError("Finalization requires metric lineage to be rebuilt after Report.")
        visualizations = [
            stage
            for stage in state["stages"]
            if stage["role"] == "growth-visualization" and stage["status"] in {"approved", "completed"}
        ]
        ready_charts = []
        for visualization in visualizations:
            visual_output = _verified_stage_output(run_dir, visualization)
            ready_charts.extend(
                chart
                for chart in visual_output.get("role_payload", {}).get("chart_specs", [])
                if chart.get("status") == "ready"
            )
        if ready_charts and "chart_manifest" not in active_kinds:
            raise ValueError("Finalization requires an active chart render manifest for ready Visualization charts.")
        for artifact in active_artifacts:
            if artifact.get("kind") not in {"metric_lineage_latest", "chart_manifest"}:
                continue
            path = resolve_within(run_dir, artifact["path"])
            if not path.is_file() or sha256_file(path) != artifact["sha256"]:
                raise ValueError(f"Finalization artifact is missing or changed: {artifact['path']}")
            if artifact.get("kind") == "metric_lineage_latest":
                lineage = load_json(path)
                _validate_metric_lineage(lineage)
            if artifact.get("kind") == "chart_manifest":
                chart_manifest = load_json(path)
                _validate_chart_manifest(chart_manifest)
                failed = [item["chart_id"] for item in chart_manifest["charts"] if item["status"] == "failed"]
                if failed:
                    raise ValueError("Finalization found failed chart renders: " + ", ".join(failed))

        final_reports = [item for item in active_artifacts if item.get("kind") == "final_report"]
        final_manifests = [item for item in active_artifacts if item.get("kind") == "final_report_manifest"]
        final_report = None
        if reports:
            if len(final_reports) != 1 or len(final_manifests) != 1:
                raise ValueError("Finalization requires one registered final report and its manifest.")
            final_report = final_reports[0]
            final_report_path = resolve_within(run_dir, final_report["path"])
            if not final_report_path.is_file() or final_report_path.stat().st_size == 0 or sha256_file(final_report_path) != final_report["sha256"]:
                raise ValueError("Registered final report is missing, empty, or changed.")
            manifest_path = resolve_within(run_dir, final_manifests[0]["path"])
            if not manifest_path.is_file() or sha256_file(manifest_path) != final_manifests[0]["sha256"]:
                raise ValueError("Registered final report manifest is missing or changed.")
            final_manifest = load_json(manifest_path)
            validate_schema(final_manifest, SCHEMA_DIR / "final-report-manifest.schema.json")
        elif final_reports or final_manifests:
            raise ValueError("Review-terminal finalization cannot contain final report artifacts.")
        current_lineage = next((item for item in reversed(active_artifacts) if item.get("kind") == "metric_lineage_latest"), None)
        current_chart_manifest = next((item for item in reversed(active_artifacts) if item.get("kind") == "chart_manifest"), None)
        if final_report is not None:
            expected_manifest_bindings = {
                "report_artifact_id": reports[0]["artifact"]["artifact_id"],
                "report_stage_sha256": reports[0]["artifact"]["sha256"],
                "review_artifact_id": review_stage["artifact"]["artifact_id"],
                "lineage_artifact_id": current_lineage["artifact_id"] if current_lineage else None,
                "chart_manifest_artifact_id": current_chart_manifest["artifact_id"] if current_chart_manifest else None,
                "final_report_path": final_report["path"],
                "final_report_sha256": final_report["sha256"],
            }
            if any(final_manifest.get(key) != expected for key, expected in expected_manifest_bindings.items()):
                raise ValueError("Final report manifest is not bound to the current Report, Review, lineage, and charts.")
        if any(lease["state"] in {"prepared", "executing", "publishing"} for lease in state.get("execution_leases", [])):
            raise ValueError("Finalization cannot proceed while an execution lease is active.")

        summary = build_run_summary(run_dir)
        finalization_errors = audit_run(run_dir)
        if finalization_errors:
            raise ValueError(
                "Run changed during finalization consistency check:\n- " + "\n- ".join(finalization_errors)
            )
        summary.pop("state_sha256", None)
        summary["status"] = "completed"
        summary["state_revision"] = state["revision"] + 1
        summary["finalization_base_state_sha256"] = sha256_json(state)
        summary["completion_kind"] = "report" if final_report is not None else "review_terminal"
        summary["final_report_artifact_id"] = final_report["artifact_id"] if final_report is not None else None
        summary["recommended_action"] = "No workflow action is required."
        output = run_dir / "final" / "summaries" / f"run-summary-r{state['revision'] + 1}.json"
        atomic_write_json(output, summary)
        record = {
            "artifact_id": new_id("run_summary"),
            "kind": "run_summary",
            "path": str(output.relative_to(run_dir.resolve())).replace("\\", "/"),
            "sha256": sha256_file(output),
            "metadata": {
                "completion_kind": summary["completion_kind"],
                "final_report_artifact_id": final_report["artifact_id"] if final_report is not None else None,
                "review_artifact_id": review_stage["artifact"]["artifact_id"],
            },
        }
        state["artifacts"].append(record)
        state["completion_kind"] = summary["completion_kind"]
        _set_status(state, "completed")
        _commit(run_dir, state, "run_finalized", {"run_summary": record})
        return state


def resume(run_dir: Path) -> dict[str, Any]:
    with RunLock(run_dir):
        state = load_state(run_dir, for_update=True)
        if state["status"] not in {"running", "validating", "finalizing", "stopped", "blocked", "failed"}:
            raise ValueError(f"Run cannot resume from status {state['status']}.")
        interrupted = state.get("current_stage") or state.get("pending_stage")
        if state["status"] in {"running", "validating"} and interrupted:
            active = _find_stage(state, interrupted)
            if active.get("attempt", 0) > 0:
                (run_dir / "stages" / interrupted / f"attempt-{active['attempt']}").mkdir(parents=True, exist_ok=True)
        errors = audit_run(run_dir)
        if errors:
            raise ValueError("Run consistency check failed:\n- " + "\n- ".join(errors))
        original_status = state["status"]
        target = state.get("resume_status") if original_status == "stopped" else original_status
        if target is None:
            raise ValueError("Stopped run has no recorded resume boundary.")
        if target in {"running", "validating"} and interrupted:
            stage = _find_stage(state, interrupted)
            if stage["status"] in {"running", "validating", "failed", "blocked"}:
                if stage.get("runtime") and stage["runtime"].get("completed_at") is None:
                    completed_at = utc_now()
                    stage["runtime"]["completed_at"] = completed_at
                    stage["runtime"]["elapsed_ms"] = _elapsed_ms(stage["runtime"]["started_at"], completed_at)
                    stage["runtime"]["failure_class"] = "interrupted"
                stage["status"] = "revising"
                state["current_stage"] = None
                state["pending_stage"] = stage["stage_id"]
                target = "revising"
        elif target in {"blocked", "failed"} and interrupted:
            if (state.get("pending_approval") or {}).get("approval_type") == "rollback":
                target = "failed"
            else:
                stage = _find_stage(state, interrupted)
                stage["status"] = "revising"
                state["current_stage"] = None
                state["pending_stage"] = stage["stage_id"]
                target = "revising"
        elif target == "initialized":
            target = "awaiting_route_confirmation"
        if target != original_status and target not in ALLOWED_TRANSITIONS[original_status]:
            raise ValueError(f"Saved resume boundary {target!r} is not legal from {original_status!r}.")
        aborted_leases = _abort_active_execution_leases(state, "resume_recovery")
        _set_status(state, target)
        state["resume_status"] = None
        _commit(run_dir, state, "run_resumed", {"status": target, "aborted_leases": aborted_leases})
        return state


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description=f"Control a Multi-Agent Data Analysis v{CURRENT_CONTRACT_VERSION} run.")
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init")
    init_parser.add_argument("--runs-root", type=Path, required=True)
    init_parser.add_argument("--run-id", required=True)
    init_parser.add_argument("--request-file", type=Path, required=True)
    init_parser.add_argument("--model-policy", choices=["native-portable", "native-explicit"], default="native-portable")

    route_parser = sub.add_parser("set-route")
    route_parser.add_argument("--run-dir", type=Path, required=True)
    route_parser.add_argument("--route-file", type=Path, required=True)

    start_parser = sub.add_parser("start-stage")
    start_parser.add_argument("--run-dir", type=Path, required=True)
    start_parser.add_argument("--stage-id", required=True)

    runtime_parser = sub.add_parser("record-agent-runtime")
    runtime_parser.add_argument("--run-dir", type=Path, required=True)
    runtime_parser.add_argument("--stage-id", required=True)
    runtime_parser.add_argument("--thread-id")
    runtime_parser.add_argument("--model")
    runtime_parser.add_argument("--input-tokens", type=int)
    runtime_parser.add_argument("--output-tokens", type=int)

    receipt_parser = sub.add_parser("record-agent-receipt")
    receipt_parser.add_argument("--run-dir", type=Path, required=True)
    receipt_parser.add_argument("--stage-id", required=True)
    receipt_parser.add_argument("--raw-response", type=Path, required=True)
    receipt_parser.add_argument("--stage-json", type=Path, required=True)
    receipt_parser.add_argument("--agent-id")
    receipt_parser.add_argument("--model")
    receipt_parser.add_argument("--input-tokens", type=int)
    receipt_parser.add_argument("--output-tokens", type=int)
    receipt_parser.add_argument("--capture-method", choices=["root_cli", "codex_tool_result"], default="root_cli")

    record_parser = sub.add_parser("record-stage")
    record_parser.add_argument("--run-dir", type=Path, required=True)
    record_parser.add_argument("--stage-json", type=Path, required=True)
    record_parser.add_argument("--stage-markdown", type=Path, required=True)
    record_parser.add_argument("--validation-report", type=Path, required=True)

    approve_parser = sub.add_parser("approve")
    approve_parser.add_argument("--run-dir", type=Path, required=True)
    approve_parser.add_argument("--type", dest="approval_type", choices=["route", "stage", "query", "rollback"], required=True)
    approve_parser.add_argument("--subject-id", required=True)
    approve_parser.add_argument("--subject-revision", type=int, required=True)
    approve_parser.add_argument("--subject-sha256", required=True)
    approve_parser.add_argument("--action", required=True)
    approve_parser.add_argument("--user-text", required=True)
    approve_parser.add_argument("--idempotency-key", required=True)
    approve_parser.add_argument("--source-surface", default="codex")

    query_parser = sub.add_parser("prepare-query")
    query_parser.add_argument("--run-dir", type=Path, required=True)
    query_parser.add_argument("--sql-file", type=Path, required=True)
    query_parser.add_argument("--query-id", required=True)
    query_parser.add_argument("--data-source-id", required=True)
    query_parser.add_argument("--data-source-fingerprint", required=True)
    query_parser.add_argument("--dialect", required=True)
    query_parser.add_argument("--timeout-seconds", type=int, default=30)
    query_parser.add_argument("--max-rows", type=int, default=1000)
    query_parser.add_argument("--max-result-bytes", type=int, default=10 * 1024 * 1024)

    ingest_parser = sub.add_parser("ingest")
    ingest_parser.add_argument("--run-dir", type=Path, required=True)
    ingest_parser.add_argument("--source", type=Path, required=True)
    ingest_parser.add_argument("--kind", required=True)
    ingest_parser.add_argument("--name")

    revise_parser = sub.add_parser("revise")
    revise_parser.add_argument("--run-dir", type=Path, required=True)
    revise_parser.add_argument("--stage-id", required=True)
    revise_parser.add_argument("--request", required=True)

    metric_edit_parser = sub.add_parser("metric-edit")
    metric_edit_parser.add_argument("--run-dir", type=Path, required=True)
    metric_edit_parser.add_argument("--edit-file", type=Path, required=True)

    workbench_export_parser = sub.add_parser("metrics-workbench-export")
    workbench_export_parser.add_argument("--run-dir", type=Path, required=True)
    workbench_export_parser.add_argument("--output", type=Path, required=True)

    workbench_import_parser = sub.add_parser("metrics-workbench-import")
    workbench_import_parser.add_argument("--run-dir", type=Path, required=True)
    workbench_import_parser.add_argument("--workbench-file", type=Path, required=True)

    stop_parser = sub.add_parser("stop")
    stop_parser.add_argument("--run-dir", type=Path, required=True)
    stop_parser.add_argument("--reason", required=True)

    resume_parser = sub.add_parser("resume")
    resume_parser.add_argument("--run-dir", type=Path, required=True)

    status_parser = sub.add_parser("status")
    status_parser.add_argument("--run-dir", type=Path, required=True)

    audit_parser = sub.add_parser("audit")
    audit_parser.add_argument("--run-dir", type=Path, required=True)

    recover_parser = sub.add_parser("recover")
    recover_parser.add_argument("--run-dir", type=Path, required=True)

    recover_executions_parser = sub.add_parser("recover-executions")
    recover_executions_parser.add_argument("--run-dir", type=Path, required=True)

    migrate_parser = sub.add_parser("migrate")
    migrate_parser.add_argument("--run-dir", type=Path, required=True)

    summary_parser = sub.add_parser("summary")
    summary_parser.add_argument("--run-dir", type=Path, required=True)
    summary_parser.add_argument("--output", type=Path)

    publish_report_parser = sub.add_parser("publish-final-report")
    publish_report_parser.add_argument("--run-dir", type=Path, required=True)

    finalize_parser = sub.add_parser("finalize")
    finalize_parser.add_argument("--run-dir", type=Path, required=True)

    args = parser.parse_args()
    try:
        if args.command == "init":
            request = load_json(args.request_file)
            run_dir = initialize_run(args.runs_root, args.run_id, request, args.model_policy)
            _print({"run_dir": str(run_dir), "state": load_state(run_dir)})
        elif args.command == "set-route":
            _print(set_route(args.run_dir, args.route_file))
        elif args.command == "start-stage":
            _print({"attempt_dir": str(start_stage(args.run_dir, args.stage_id))})
        elif args.command == "record-agent-runtime":
            _print(record_agent_runtime(args.run_dir, args.stage_id, args.thread_id, args.model, args.input_tokens, args.output_tokens))
        elif args.command == "record-agent-receipt":
            _print(
                record_agent_receipt(
                    args.run_dir,
                    args.stage_id,
                    args.raw_response,
                    args.stage_json,
                    args.agent_id,
                    args.model,
                    args.input_tokens,
                    args.output_tokens,
                    args.capture_method,
                )
            )
        elif args.command == "record-stage":
            _print(record_stage(args.run_dir, args.stage_json, args.stage_markdown, args.validation_report))
        elif args.command == "approve":
            _print(
                approve(
                    args.run_dir,
                    args.approval_type,
                    args.subject_id,
                    args.subject_revision,
                    args.subject_sha256,
                    args.action,
                    args.user_text,
                    args.idempotency_key,
                    args.source_surface,
                )
            )
        elif args.command == "prepare-query":
            _print(
                prepare_query(
                    args.run_dir,
                    args.sql_file,
                    args.query_id,
                    args.data_source_id,
                    args.dialect,
                    args.timeout_seconds,
                    args.max_rows,
                    args.max_result_bytes,
                    args.data_source_fingerprint,
                )
            )
        elif args.command == "ingest":
            _print(ingest_artifact(args.run_dir, args.source, args.kind, args.name))
        elif args.command == "revise":
            _print(revise(args.run_dir, args.stage_id, args.request))
        elif args.command == "metric-edit":
            _print(request_metric_edit(args.run_dir, args.edit_file))
        elif args.command == "metrics-workbench-export":
            _print({"path": str(export_metrics_workbench(args.run_dir, args.output))})
        elif args.command == "metrics-workbench-import":
            _print(import_metrics_workbench(args.run_dir, args.workbench_file))
        elif args.command == "stop":
            _print(stop(args.run_dir, args.reason))
        elif args.command == "resume":
            _print(resume(args.run_dir))
        elif args.command == "status":
            _print(load_state(args.run_dir))
        elif args.command == "audit":
            errors = audit_run(args.run_dir)
            _print({"valid": not errors, "errors": errors})
            return 0 if not errors else 2
        elif args.command == "recover":
            _print(recover_state(args.run_dir))
        elif args.command == "recover-executions":
            _print(recover_execution_leases(args.run_dir))
        elif args.command == "migrate":
            _print(migrate_run(args.run_dir))
        elif args.command == "summary":
            _print(build_run_summary(args.run_dir, args.output))
        elif args.command == "publish-final-report":
            _print(publish_final_report(args.run_dir))
        elif args.command == "finalize":
            _print(finalize_run(args.run_dir))
    except (OSError, ValueError, TimeoutError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
