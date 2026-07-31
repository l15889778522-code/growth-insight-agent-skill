#!/usr/bin/env python3
"""Deterministic run state, approval, revision, and recovery controller."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

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
from validate_stage_output import validate_stage


RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
ARTIFACT_KIND_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
STATE_FILE = "run-state.json"
EVENTS_FILE = "events.jsonl"
APPROVALS_FILE = "approvals.jsonl"

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


def _elapsed_ms(started_at: str, completed_at: str) -> int:
    started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    completed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    return max(0, int((completed - started).total_seconds() * 1000))


def _state_path(run_dir: Path) -> Path:
    return run_dir / STATE_FILE


def _assert_state_head(run_dir: Path, state: dict[str, Any]) -> None:
    events = read_jsonl(run_dir / EVENTS_FILE)
    if not events:
        raise ValueError("Run has no committed event head; run recover before continuing.")
    chain_errors = _audit_event_chain(events)
    if chain_errors:
        raise ValueError("Event chain authentication failed:\n- " + "\n- ".join(chain_errors))
    if state.get("last_event_id") != len(events) or sha256_json(state) != events[-1].get("state_sha256"):
        raise ValueError("run-state.json does not match the last committed event; run recover before continuing.")


def load_state(run_dir: Path, *, authenticate: bool = True) -> dict[str, Any]:
    state = load_json(_state_path(run_dir))
    if not isinstance(state, dict):
        raise ValueError("run-state.json must contain an object.")
    validate_schema(state, SCHEMA_DIR / "run-state.schema.json")
    if authenticate:
        _assert_state_head(run_dir, state)
    return state


def _set_status(state: dict[str, Any], target: str) -> None:
    current = state["status"]
    if target == current:
        return
    if target not in ALLOWED_TRANSITIONS.get(current, set()):
        raise ValueError(f"Illegal state transition: {current} -> {target}")
    state["status"] = target


def _commit(run_dir: Path, state: dict[str, Any], event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
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
    event = {
        "schema_version": "1.1",
        "event_id": state["last_event_id"],
        "event_type": event_type,
        "run_id": state["run_id"],
        "state_revision": state["revision"],
        "created_at": state["updated_at"],
        "status": state["status"],
        "previous_state_sha256": previous_hash,
        "state_sha256": state_hash,
        "state_after": deepcopy(state),
        "payload": payload or {},
    }
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
    state: dict[str, Any] = {
        "schema_version": "1.1",
        "run_id": run_id,
        "revision": 0,
        "status": "initialized",
        "runtime": {
            "kind": "codex_native",
            "model_policy": model_policy,
            "agent_config_hashes": agent_hashes,
            "resolved_models": resolved_models,
            "agent_settings": agent_settings,
            "surface": None,
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
        "artifacts": [],
        "data_source": None,
        "last_event_id": 0,
        "resume_status": None,
        "created_at": now,
        "updated_at": now,
    }
    atomic_write_json(run_dir / "request.json", request)
    _commit(run_dir, state, "run_initialized", {"request_sha256": sha256_json(request)})
    return run_dir


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
    return route


def _next_pending_stage(state: dict[str, Any]) -> str | None:
    for stage in state["stages"]:
        if stage["status"] in {"pending", "revising", "stale"}:
            return stage["stage_id"]
    return None


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
        state = load_state(run_dir)
        if state["status"] == "completed":
            raise ValueError("Completed run cannot be rerouted.")
        if state.get("current_stage") or any(item["status"] == "running" for item in state["stages"]):
            raise ValueError("Cannot replace the route while a stage is running.")
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
        _commit(run_dir, state, "route_proposed", {"route_sha256": route_hash})
        return state


def start_stage(run_dir: Path, stage_id: str) -> Path:
    with RunLock(run_dir):
        state = load_state(run_dir)
        if state["status"] not in {"running", "revising"}:
            raise ValueError(f"Cannot start a stage while run status is {state['status']}.")
        if any(item["status"] == "running" for item in state["stages"]):
            raise ValueError("Another stage is already running.")
        if state.get("pending_approval"):
            raise ValueError("A pending route, stage, or rollback approval must be resolved before a stage can start.")
        if state.get("pending_query"):
            raise ValueError("A prepared query must finish or be revised before another stage can start.")
        _load_verified_route(run_dir, state, require_executable=True)
        if state.get("pending_stage") != stage_id:
            raise ValueError(f"The only startable stage is {state.get('pending_stage')!r}.")
        stage = _find_stage(state, stage_id)
        if stage["status"] not in {"pending", "revising", "stale"}:
            raise ValueError(f"Stage {stage_id} cannot start from status {stage['status']}.")
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
            "model": state["runtime"]["resolved_models"].get(stage["role"]),
            "model_reasoning_effort": settings.get("model_reasoning_effort"),
            "token_usage": None,
            "failure_class": None,
        }
        stage["input_hashes"] = {
            dependency: _find_stage(state, dependency)["artifact"]["sha256"]
            for dependency in stage["depends_on"]
            if _find_stage(state, dependency).get("artifact")
        }
        state["current_stage"] = stage_id
        state["pending_stage"] = None
        state["pending_approval"] = None
        _set_status(state, "running")
        _commit(run_dir, state, "stage_started", {"stage_id": stage_id, "role": stage["role"], "attempt": stage["attempt"]})
        attempt_dir = run_dir / "stages" / f"{stage_id}" / f"attempt-{stage['attempt']}"
        attempt_dir.mkdir(parents=True, exist_ok=False)
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
        state = load_state(run_dir)
        stage = _find_stage(state, stage_id)
        if stage["status"] != "running" or not stage.get("runtime"):
            raise ValueError(f"Stage {stage_id} has no active Agent runtime to update.")
        runtime = stage["runtime"]
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


def _load_current_role_output(run_dir: Path, state: dict[str, Any], role: str) -> dict[str, Any] | None:
    candidates = [
        item
        for item in state["stages"]
        if item["role"] == role and item.get("artifact") and item["status"] in {"approved", "completed"}
    ]
    if not candidates:
        return None
    return _verified_stage_output(run_dir, candidates[-1])


def _validate_stage_context(run_dir: Path, state: dict[str, Any], stage: dict[str, Any], value: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for dependency, expected_hash in stage["input_hashes"].items():
        current = _find_stage(state, dependency).get("artifact")
        if not current or current["sha256"] != expected_hash:
            errors.append(f"Approved input {dependency} changed while the stage was running.")

    ancestor_ids = _descendants_reverse(state, stage["stage_id"])
    prior_decisions: dict[bytes, Any] = {}
    for prior_stage in state["stages"]:
        if prior_stage["stage_id"] not in ancestor_ids or prior_stage["status"] != "approved" or not prior_stage.get("artifact"):
            continue
        prior_output = _verified_stage_output(run_dir, prior_stage)
        for decision in prior_output.get("confirmed_decisions", []):
            prior_decisions.setdefault(json.dumps(decision, ensure_ascii=False, sort_keys=True).encode("utf-8"), decision)
    current_decisions = {
        json.dumps(decision, ensure_ascii=False, sort_keys=True).encode("utf-8")
        for decision in value.get("confirmed_decisions", [])
    }
    missing_decisions = [decision for key, decision in prior_decisions.items() if key not in current_decisions]
    if missing_decisions:
        errors.append("Stage output omitted approved confirmed_decisions: " + json.dumps(missing_decisions, ensure_ascii=False, sort_keys=True))

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
        referenced = []
        for mapping in payload.get("field_mappings", []):
            referenced.append(mapping.get("metric_id"))
        for query in payload.get("queries", []):
            referenced.extend(query.get("metric_ids", []))
        unknown = sorted({item for item in referenced if item not in known_metrics})
        if unknown:
            errors.append("SQL output references unknown metric IDs: " + ", ".join(unknown))

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
    artifact_paths = {
        item.get("path")
        for item in state["artifacts"]
        if item.get("path") and not item.get("superseded_by")
    }
    approved_stage_ids = {
        item["stage_id"]
        for item in state["stages"]
        if item["status"] in {"approved", "completed"}
    }

    def evidence_resolves(reference: str) -> bool:
        path_part = reference.split("#", 1)[0]
        return path_part in artifact_paths or any(reference.startswith(stage_id + ":") for stage_id in approved_stage_ids)

    evidence_items = []
    if role == "growth-insight":
        evidence_items.extend(payload.get("observations", []))
        evidence_items.extend(payload.get("recommendations", []))
    elif role == "growth-review":
        evidence_items.extend(payload.get("findings", []))
    elif role == "growth-report":
        evidence_items.extend(payload.get("recommendations", []))
    unresolved_refs = sorted(
        {
            reference
            for item in evidence_items
            for reference in item.get("evidence_refs", [])
            if not evidence_resolves(reference)
        }
    )
    if unresolved_refs:
        errors.append(f"{role} output has unresolved evidence references: " + ", ".join(unresolved_refs))

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
        metric_stages = [
            item
            for item in state["stages"]
            if item["role"] == "growth-metrics" and item["status"] in {"approved", "completed"}
        ]
        if metric_stages:
            lineage_records = [
                item
                for item in state["artifacts"]
                if item.get("kind") == "metric_lineage_latest" and not item.get("superseded_by")
            ]
            if not lineage_records:
                errors.append("Review requires a current registered metric lineage artifact.")
            else:
                lineage_record = lineage_records[-1]
                lineage_path = resolve_within(run_dir, lineage_record["path"])
                if not lineage_path.is_file() or sha256_file(lineage_path) != lineage_record["sha256"]:
                    errors.append("Registered metric lineage is missing or changed.")
                else:
                    lineage = load_json(lineage_path)
                    expected_breaks = {
                        f"{metric['metric_id']}:{lineage_break}"
                        for metric in lineage.get("metrics", [])
                        for lineage_break in metric.get("breaks", [])
                    }
                    declared_breaks = set(payload.get("lineage_breaks", []))
                    if declared_breaks != expected_breaks:
                        errors.append("Review lineage_breaks must exactly match the registered metric lineage.")
        if decision == "PASS" and payload.get("lineage_breaks"):
            errors.append("Review cannot PASS while metric lineage breaks remain.")
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
            caveats = payload.get("caveats", [])
            missing = [item for item in required_caveats if item not in caveats]
            if missing:
                errors.append("Report omitted Review caveats: " + "; ".join(missing))
    return errors


def record_stage(run_dir: Path, stage_json: Path, stage_markdown: Path, validation_report: Path) -> dict[str, Any]:
    with RunLock(run_dir):
        state = load_state(run_dir)
        if state["status"] != "running" or not state["current_stage"]:
            raise ValueError("No running stage is available to record.")
        stage = _find_stage(state, state["current_stage"])
        value = load_json(stage_json)
        if not isinstance(value, dict):
            raise ValueError("Stage JSON must contain an object.")
        errors = validate_stage(value, stage["role"], state["run_id"], stage["stage_id"], stage["attempt"])
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
        resolved_paths = [resolve_within(run_dir, path) for path in (stage_json, stage_markdown, validation_report)]
        for resolved in resolved_paths:
            if not resolved.is_file():
                raise ValueError(f"Required stage artifact does not exist: {resolved}")
        stage_json, stage_markdown, validation_report = resolved_paths
        validation = load_json(validation_report)
        if not isinstance(validation, dict) or not validation.get("valid"):
            raise ValueError("Validation report must record a successful stage validation.")
        if validation.get("role") != stage["role"] or validation.get("stage_sha256") != sha256_json(value):
            raise ValueError("Validation report does not match the current stage artifact.")
        _set_status(state, "validating")
        artifact_hash = sha256_file(stage_json)
        artifact = {
            "revision": stage["attempt"],
            "json_path": str(stage_json.resolve().relative_to(run_dir.resolve())).replace("\\", "/"),
            "markdown_path": str(stage_markdown.resolve().relative_to(run_dir.resolve())).replace("\\", "/"),
            "markdown_sha256": sha256_file(stage_markdown),
            "validation_path": str(validation_report.resolve().relative_to(run_dir.resolve())).replace("\\", "/"),
            "validation_sha256": sha256_file(validation_report),
            "sha256": artifact_hash,
            "stage_status": value["stage_status"],
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
                    "schema_version": "1.1",
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
        elif stage["role"] == "growth-report":
            stage["status"] = "completed"
            state["pending_approval"] = None
            state["current_stage"] = None
            state["pending_stage"] = None
            _set_status(state, "finalizing")
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
) -> dict[str, Any]:
    with RunLock(run_dir):
        state = load_state(run_dir)
        existing = next((item for item in read_jsonl(run_dir / APPROVALS_FILE) if item.get("idempotency_key") == idempotency_key), None)
        approval_already_appended = existing is not None
        if existing:
            fingerprint = (approval_type, subject_id, subject_revision, subject_sha256, action, user_text)
            prior = tuple(existing.get(key) for key in ("approval_type", "subject_id", "subject_revision", "subject_sha256", "action", "user_text"))
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
                "schema_version": "1.1",
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
                "query": None,
            }
            if approval_type == "query":
                pending_query = state.get("pending_query") or {}
                approval["query"] = {
                    "query_sha256": pending_query.get("query_sha256"),
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
    from sql_guard import validate_and_rewrite

    raw_sql = sql_file.read_text(encoding="utf-8")
    if not re.fullmatch(r"[A-Fa-f0-9]{64}", data_source_fingerprint):
        raise ValueError("data_source_fingerprint must be a 64-character SHA-256 value.")
    result = validate_and_rewrite(raw_sql, dialect=dialect, max_rows=max_rows)
    if not result.ok:
        raise ValueError("Query failed SQL safety validation:\n- " + "\n- ".join(result.errors))
    with RunLock(run_dir):
        state = load_state(run_dir)
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
        data_dir = run_dir / "data"
        final_sql_path = data_dir / "query.sql"
        atomic_write_text(final_sql_path, result.sql)
        sql_hash = sha256_file(final_sql_path)
        revision = 1
        old_request = data_dir / "query-request.json"
        if old_request.exists():
            revision = int(load_json(old_request).get("revision", 0)) + 1
        fingerprint = {
            "query_id": query_id,
            "revision": revision,
            "sql_sha256": sql_hash,
            "data_source_id": data_source_id,
            "data_source_fingerprint": data_source_fingerprint.lower(),
            "dialect": dialect,
            "timeout_seconds": timeout_seconds,
            "max_rows": max_rows,
            "max_result_bytes": max_result_bytes,
        }
        request = {
            "schema_version": "1.1",
            **fingerprint,
            "sql_path": "data/query.sql",
            "subject_sha256": sha256_json(fingerprint),
            "warnings": result.warnings,
            "created_at": utc_now(),
        }
        validate_schema(request, SCHEMA_DIR / "query-request.schema.json")
        atomic_write_json(old_request, request)
        state["pending_query"] = {
            "approval_type": "query",
            "subject_id": query_id,
            "subject_revision": revision,
            "subject_sha256": request["subject_sha256"],
            "query_sha256": sql_hash,
            "data_source_id": data_source_id,
            "data_source_fingerprint": data_source_fingerprint.lower(),
            "dialect": dialect,
            "timeout_seconds": timeout_seconds,
            "max_rows": max_rows,
            "max_result_bytes": max_result_bytes,
            "approved": False,
            "approval_id": None,
        }
        state["data_source"] = {
            "data_source_id": data_source_id,
            "data_source_fingerprint": data_source_fingerprint.lower(),
            "dialect": dialect,
        }
        _set_status(state, "awaiting_query_confirmation")
        _commit(run_dir, state, "query_prepared", {"query_request": request})
        return request


def record_query_result(run_dir: Path, manifest_path: Path, result_path: Path, profile_path: Path) -> dict[str, Any]:
    with RunLock(run_dir):
        state = load_state(run_dir)
        pending = state.get("pending_query")
        if not pending or not pending.get("approved"):
            raise ValueError("The current query has not been approved.")
        manifest_path = resolve_within(run_dir, manifest_path)
        result_path = resolve_within(run_dir, result_path)
        profile_path = resolve_within(run_dir, profile_path)
        manifest = load_json(manifest_path)
        validate_schema(manifest, SCHEMA_DIR / "query-manifest.schema.json")
        expected_manifest = {
            "query_id": pending.get("subject_id"),
            "sql_sha256": pending.get("query_sha256"),
            "data_source_id": pending.get("data_source_id"),
            "data_source_fingerprint": pending.get("data_source_fingerprint"),
            "dialect": pending.get("dialect"),
            "timeout_seconds": pending.get("timeout_seconds"),
            "max_rows": pending.get("max_rows"),
            "max_result_bytes": pending.get("max_result_bytes"),
        }
        if any(manifest.get(key) != value for key, value in expected_manifest.items()):
            raise ValueError("Query manifest does not match the approved request.")
        expected_result_path = str(result_path.relative_to(run_dir.resolve())).replace("\\", "/")
        expected_profile_path = str(profile_path.relative_to(run_dir.resolve())).replace("\\", "/")
        if manifest["result_path"] != expected_result_path or manifest["profile_path"] != expected_profile_path:
            raise ValueError("Query manifest paths do not match the recorded files.")
        if manifest["result_bytes"] != result_path.stat().st_size:
            raise ValueError("Query result byte count does not match the manifest.")
        if sha256_file(result_path) != manifest["result_sha256"] or sha256_file(profile_path) != manifest["profile_sha256"]:
            raise ValueError("Query result or profile hash does not match the manifest.")
        artifacts = []
        for kind, path in (("query_manifest", manifest_path), ("query_result", result_path), ("result_profile", profile_path)):
            resolved = resolve_within(run_dir, path)
            if not resolved.is_file():
                raise ValueError(f"Missing query artifact: {resolved}")
            artifact = {
                "artifact_id": new_id(kind),
                "kind": kind,
                "path": str(resolved.relative_to(run_dir.resolve())).replace("\\", "/"),
                "sha256": sha256_file(resolved),
            }
            for old in state["artifacts"]:
                if old.get("kind") == kind and old.get("path") == artifact["path"] and not old.get("superseded_by"):
                    old["superseded_by"] = artifact["artifact_id"]
            state["artifacts"].append(artifact)
            artifacts.append(artifact)
        state["pending_query"] = None
        _set_status(state, "running")
        _commit(run_dir, state, "query_completed", {"query_id": manifest["query_id"], "artifacts": artifacts})
        return state


def register_artifacts(run_dir: Path, artifacts: list[dict[str, Any]], event_type: str = "artifacts_registered") -> list[dict[str, Any]]:
    with RunLock(run_dir):
        state = load_state(run_dir)
        if state["status"] == "completed":
            raise ValueError("Completed run artifacts are immutable.")
        records: list[dict[str, Any]] = []
        existing = {
            (item.get("kind"), item.get("path"), item.get("sha256"))
            for item in state["artifacts"]
            if not item.get("superseded_by")
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
            record = {
                "artifact_id": new_id(kind),
                "kind": kind,
                "path": relative,
                "sha256": digest,
            }
            if item.get("metadata") is not None:
                record["metadata"] = item["metadata"]
            for old in state["artifacts"]:
                if old.get("kind") == kind and old.get("path") == relative and not old.get("superseded_by"):
                    old["superseded_by"] = record["artifact_id"]
            state["artifacts"].append(record)
            records.append(record)
            existing.add(key)
        if records:
            _commit(run_dir, state, event_type, {"artifacts": records})
        return records


def invalidate_artifacts(run_dir: Path, kinds: set[str], reason: str) -> list[str]:
    with RunLock(run_dir):
        state = load_state(run_dir)
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
    if load_state(run_dir)["status"] == "completed":
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
    state = load_state(run_dir)
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
        if stage["stage_id"] in affected
    }
    kinds: set[str] = {"run_summary"}
    if affected_roles & {"growth-business", "growth-metrics", "growth-sql"}:
        kinds.update({"query_manifest", "query_result", "result_profile"})
    if affected_roles & {"growth-business", "growth-metrics", "growth-sql", "growth-insight", "growth-visualization"}:
        kinds.update({"chart_specs", "chart_manifest", "chart_png", "chart_html"})
    if affected_roles & {"growth-metrics", "growth-sql", "growth-insight", "growth-visualization", "growth-review", "growth-report"}:
        kinds.update({"metric_lineage", "metric_lineage_latest"})
    invalidation_id = new_id("invalidation")
    invalidated: list[str] = []
    for artifact in state["artifacts"]:
        if artifact.get("kind") in kinds and not artifact.get("superseded_by"):
            artifact["superseded_by"] = invalidation_id
            invalidated.append(artifact["artifact_id"])
    return invalidated


def _revise_state(state: dict[str, Any], stage_id: str) -> tuple[dict[str, Any], set[str], list[str]]:
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
        state = load_state(run_dir)
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
        state = load_state(run_dir)
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
        editable_fields = {
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


def stop(run_dir: Path, reason: str) -> dict[str, Any]:
    with RunLock(run_dir):
        state = load_state(run_dir)
        if state["status"] == "completed":
            raise ValueError("Completed run cannot be stopped.")
        state["resume_status"] = state["status"]
        _set_status(state, "stopped")
        _commit(run_dir, state, "run_stopped", {"reason": reason})
        return state


def _audit_event_chain(events: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    previous_hash: str | None = None
    for index, event in enumerate(events, start=1):
        if event.get("event_id") != index:
            errors.append("events.jsonl event_id sequence is not contiguous.")
            break
        if event.get("state_revision") != index:
            errors.append(f"Event {index} has an unexpected state_revision.")
        snapshot = event.get("state_after")
        if not isinstance(snapshot, dict):
            errors.append(f"Event {index} is missing its recoverable state snapshot.")
            continue
        try:
            validate_schema(snapshot, SCHEMA_DIR / "run-state.schema.json")
        except ValueError as exc:
            errors.append(f"Event {index} state snapshot is invalid: {exc}")
            continue
        snapshot_hash = sha256_json(snapshot)
        if event.get("state_sha256") != snapshot_hash:
            errors.append(f"Event {index} state snapshot hash does not match.")
        if event.get("previous_state_sha256") != previous_hash:
            errors.append(f"Event {index} does not continue the state hash chain.")
        if snapshot.get("revision") != index or snapshot.get("last_event_id") != index:
            errors.append(f"Event {index} snapshot revision counters do not match.")
        if event.get("status") != snapshot.get("status"):
            errors.append(f"Event {index} status does not match its state snapshot.")
        previous_hash = snapshot_hash
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
    for artifact in state["artifacts"]:
        if artifact.get("superseded_by"):
            continue
        try:
            relative = artifact.get("json_path") or artifact.get("path")
            if not relative:
                raise ValueError("Artifact has neither json_path nor path.")
            path = resolve_within(run_dir, relative)
            if not path.is_file():
                errors.append(f"Missing artifact: {relative}")
            elif sha256_file(path) != artifact["sha256"]:
                errors.append(f"Artifact hash mismatch: {relative}")
            for path_key, hash_key in (("markdown_path", "markdown_sha256"), ("validation_path", "validation_sha256")):
                if path_key in artifact:
                    companion = resolve_within(run_dir, artifact[path_key])
                    if not companion.is_file():
                        errors.append(f"Missing artifact: {artifact[path_key]}")
                    elif sha256_file(companion) != artifact[hash_key]:
                        errors.append(f"Artifact hash mismatch: {artifact[path_key]}")
        except (KeyError, ValueError) as exc:
            errors.append(str(exc))
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
        if len(summaries) != 1:
            errors.append("Completed run is missing an active run_summary artifact.")
        else:
            try:
                summary = load_json(resolve_within(run_dir, summaries[0]["path"]))
                if (
                    summary.get("run_id") != state["run_id"]
                    or summary.get("status") != "completed"
                    or summary.get("state_revision") != state["revision"]
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
        errors = _audit_event_chain(events)
        if errors:
            raise ValueError("Event log cannot be recovered:\n- " + "\n- ".join(errors))
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
        recovered = deepcopy(events[-1]["state_after"])
        atomic_write_json(_state_path(run_dir), recovered)
        errors = audit_run(run_dir)
        if errors:
            raise ValueError("State snapshot was restored, but the run still has consistency errors:\n- " + "\n- ".join(errors))
        return recovered


def build_run_summary(run_dir: Path, output: Path | None = None) -> dict[str, Any]:
    state = load_state(run_dir)
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
        "schema_version": "1.1",
        "generated_at": utc_now(),
        "run_id": state["run_id"],
        "state_revision": state["revision"],
        "state_sha256": sha256_json(state),
        "status": state["status"],
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


def finalize_run(run_dir: Path) -> dict[str, Any]:
    with RunLock(run_dir):
        state = load_state(run_dir)
        if state["status"] != "finalizing":
            raise ValueError(f"Run cannot finalize from status {state['status']}.")
        consistency_errors = audit_run(run_dir)
        if consistency_errors:
            raise ValueError("Run cannot finalize with consistency errors:\n- " + "\n- ".join(consistency_errors))
        reports = [
            stage
            for stage in state["stages"]
            if stage["role"] == "growth-report" and stage["status"] == "completed"
        ]
        report_stages = [stage for stage in state["stages"] if stage["role"] == "growth-report"]
        if report_stages:
            if len(reports) != 1:
                raise ValueError("Finalization requires exactly one completed Report stage.")
            _verified_stage_output(run_dir, reports[0])
        elif any(stage["status"] not in {"approved", "completed", "skipped"} for stage in state["stages"]):
            raise ValueError("Finalization requires every routed stage to be approved, completed, or skipped.")
        active_artifacts = [item for item in state["artifacts"] if not item.get("superseded_by")]
        active_kinds = {item.get("kind") for item in active_artifacts}
        if any(stage["role"] == "growth-metrics" and stage["status"] in {"approved", "completed"} for stage in state["stages"]):
            if "metric_lineage_latest" not in active_kinds:
                raise ValueError("Finalization requires rebuilt metric lineage after Report.")
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
                validate_schema(lineage, SCHEMA_DIR / "metric-lineage.schema.json")
            if artifact.get("kind") == "chart_manifest":
                chart_manifest = load_json(path)
                validate_schema(chart_manifest, SCHEMA_DIR / "chart-render-manifest.schema.json")
                failed = [item["chart_id"] for item in chart_manifest["charts"] if item["status"] == "failed"]
                if failed:
                    raise ValueError("Finalization found failed chart renders: " + ", ".join(failed))

        summary = build_run_summary(run_dir)
        summary.pop("state_sha256", None)
        summary["status"] = "completed"
        summary["state_revision"] = state["revision"] + 1
        summary["finalization_base_state_sha256"] = sha256_json(state)
        summary["recommended_action"] = "No workflow action is required."
        output = run_dir / "final" / "run-summary.json"
        atomic_write_json(output, summary)
        record = {
            "artifact_id": new_id("run_summary"),
            "kind": "run_summary",
            "path": "final/run-summary.json",
            "sha256": sha256_file(output),
        }
        for old in state["artifacts"]:
            if old.get("kind") == "run_summary" and not old.get("superseded_by"):
                old["superseded_by"] = record["artifact_id"]
        state["artifacts"].append(record)
        _set_status(state, "completed")
        _commit(run_dir, state, "run_finalized", {"run_summary": record})
        return state


def resume(run_dir: Path) -> dict[str, Any]:
    with RunLock(run_dir):
        state = load_state(run_dir)
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
        _set_status(state, target)
        state["resume_status"] = None
        _commit(run_dir, state, "run_resumed", {"status": target})
        return state


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="Control a Multi-Agent Data Analysis v1.1 run.")
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

    summary_parser = sub.add_parser("summary")
    summary_parser.add_argument("--run-dir", type=Path, required=True)
    summary_parser.add_argument("--output", type=Path)

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
        elif args.command == "record-stage":
            _print(record_stage(args.run_dir, args.stage_json, args.stage_markdown, args.validation_report))
        elif args.command == "approve":
            _print(approve(args.run_dir, args.approval_type, args.subject_id, args.subject_revision, args.subject_sha256, args.action, args.user_text, args.idempotency_key))
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
        elif args.command == "summary":
            _print(build_run_summary(args.run_dir, args.output))
        elif args.command == "finalize":
            _print(finalize_run(args.run_dir))
    except (OSError, ValueError, TimeoutError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
