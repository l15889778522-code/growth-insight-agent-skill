#!/usr/bin/env python3
"""Validate route structure and dependency rules before any role is spawned."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from contracts import CURRENT_CONTRACT_VERSION
from runtime_common import SCHEMA_DIR, atomic_write_json, load_json, schema_errors, sha256_json, utc_now


PARALLEL_ELIGIBLE_ROLES = {
    "growth-business",
    "growth-metrics",
    "growth-insight",
    "growth-review",
    "growth-visualization",
}


def _ancestors(stage_id: str, by_id: dict[str, dict[str, Any]]) -> set[str]:
    found: set[str] = set()
    pending = list(by_id[stage_id].get("depends_on", []))
    while pending:
        current = pending.pop()
        if current in found or current not in by_id:
            continue
        found.add(current)
        pending.extend(by_id[current].get("depends_on", []))
    return found


def _validate_input_bindings(plan: dict[str, Any], errors: list[str], prefix: str = "") -> None:
    provided = plan.get("provided_inputs", [])
    bindings = plan.get("input_bindings", [])
    if plan.get("schema_version") != CURRENT_CONTRACT_VERSION and not bindings:
        return
    names = [item.get("input_name") for item in bindings if isinstance(item, dict)]
    if len(names) != len(set(names)):
        errors.append(f"{prefix}input_bindings: input_name values must be unique.")
    if set(names) != set(provided):
        errors.append(f"{prefix}input_bindings must bind every provided input exactly once.")


def validate_parallel_policy(stage: dict[str, Any], errors: list[str]) -> None:
    policy = stage.get("parallel")
    if policy is None or not isinstance(policy, dict):
        return
    enabled = policy.get("enabled")
    max_agents = policy.get("max_agents")
    merge_required = policy.get("merge_required")
    stage_id = stage.get("stage_id", "<unknown>")
    role = stage.get("role")
    if enabled:
        if max_agents != 2:
            errors.append(f"{stage_id}: enabled stage-local parallelism must allow exactly 2 Agents.")
        if merge_required is not True:
            errors.append(f"{stage_id}: parallel branches must be merged before the stage can be recorded.")
        if role not in PARALLEL_ELIGIBLE_ROLES:
            errors.append(f"{stage_id}: {role} cannot use stage-local parallel Agents.")
    else:
        if max_agents != 1:
            errors.append(f"{stage_id}: disabled parallelism must set max_agents to 1.")
        if merge_required is not False:
            errors.append(f"{stage_id}: disabled parallelism must set merge_required to false.")


def validate_route(plan: dict[str, Any], require_executable: bool = False) -> tuple[list[str], list[str]]:
    errors = schema_errors(plan, SCHEMA_DIR / "route-plan.schema.json")
    warnings: list[str] = []
    stages = plan.get("stages")
    if not isinstance(stages, list):
        return errors, warnings

    ids = [stage.get("stage_id") for stage in stages if isinstance(stage, dict)]
    sequences = [stage.get("sequence") for stage in stages if isinstance(stage, dict)]
    roles = [stage.get("role") for stage in stages if isinstance(stage, dict)]
    if len(ids) != len(set(ids)):
        errors.append("stages: stage_id values must be unique.")
    if len(sequences) != len(set(sequences)):
        errors.append("stages: sequence values must be unique.")
    if all(isinstance(value, int) for value in sequences) and sorted(sequences) != list(range(1, len(sequences) + 1)):
        errors.append("stages: sequence values must be contiguous and start at 1.")
    if len(roles) != len(set(roles)):
        errors.append("stages: initial route cannot contain the same role more than once; revisions use attempts.")

    by_id = {stage["stage_id"]: stage for stage in stages if isinstance(stage, dict) and isinstance(stage.get("stage_id"), str)}
    for stage in stages:
        if not isinstance(stage, dict) or stage.get("stage_id") not in by_id:
            continue
        validate_parallel_policy(stage, errors)
        for dependency in stage.get("depends_on", []):
            if dependency not in by_id:
                errors.append(f"{stage['stage_id']}: unknown dependency {dependency}.")
                continue
            if by_id[dependency].get("sequence", 0) >= stage.get("sequence", 0):
                errors.append(f"{stage['stage_id']}: dependency {dependency} must appear earlier in the route.")
            if by_id[dependency].get("status") == "skipped":
                errors.append(f"{stage['stage_id']}: cannot depend on skipped stage {dependency}.")

    review_ids = {stage["stage_id"] for stage in stages if isinstance(stage, dict) and stage.get("role") == "growth-review"}
    for stage in stages:
        if not isinstance(stage, dict) or stage.get("role") != "growth-report" or stage.get("stage_id") not in by_id:
            continue
        eligible_reviews = {review_id for review_id in review_ids if by_id[review_id].get("status") != "skipped"}
        if not (_ancestors(stage["stage_id"], by_id) & eligible_reviews):
            errors.append(f"{stage['stage_id']}: growth-report requires a growth-review ancestor.")

    required_inputs = set(plan.get("required_inputs", []))
    provided_inputs = set(plan.get("provided_inputs", []))
    declared_missing = set(plan.get("missing_inputs", []))
    expected_missing = required_inputs - provided_inputs
    if declared_missing != expected_missing:
        errors.append("missing_inputs must exactly equal required_inputs minus provided_inputs.")
    _validate_input_bindings(plan, errors)

    fallback = plan.get("fallback_route")
    if isinstance(fallback, dict):
        fallback_required = set(fallback.get("required_inputs", []))
        fallback_provided = set(fallback.get("provided_inputs", []))
        fallback_missing = set(fallback.get("missing_inputs", []))
        if fallback_missing != fallback_required - fallback_provided:
            errors.append("fallback_route.missing_inputs must exactly equal required_inputs minus provided_inputs.")
        fallback_plan = {"schema_version": plan.get("schema_version"), **fallback}
        _validate_input_bindings(fallback_plan, errors, "fallback_route.")

    missing_inputs = plan.get("missing_inputs", [])
    if missing_inputs:
        message = "Route is not executable because required input is missing: " + ", ".join(str(item) for item in missing_inputs)
        if require_executable:
            errors.append(message)
        else:
            warnings.append(message)

    declared_missing_set = set(missing_inputs)
    for stage in stages:
        if not isinstance(stage, dict) or stage.get("status") == "skipped" or stage.get("stage_id") not in by_id:
            continue
        ancestors = _ancestors(stage["stage_id"], by_id)
        ancestor_outputs = {
            output
            for ancestor_id in ancestors
            if by_id[ancestor_id].get("status") != "skipped"
            for output in by_id[ancestor_id].get("expected_outputs", [])
        }
        unavailable = set(stage.get("required_inputs", [])) - provided_inputs - ancestor_outputs
        for required_input in sorted(unavailable):
            message = (
                f"{stage['stage_id']}: required input {required_input!r} is neither provided "
                "nor produced by a declared ancestor."
            )
            if required_input in declared_missing_set and not require_executable:
                warnings.append(message)
            else:
                errors.append(message)

    reused_items = plan.get("reused_approved_artifacts", [])
    reused_ids = [item.get("stage_id") for item in reused_items if isinstance(item, dict)]
    if len(reused_ids) != len(set(reused_ids)):
        errors.append("reused_approved_artifacts: stage_id values must be unique.")
    for reused in reused_items:
        if not isinstance(reused, dict):
            continue
        stage = by_id.get(reused.get("stage_id"))
        if stage is None:
            errors.append(f"reused_approved_artifacts: unknown stage {reused.get('stage_id')}.")
        elif stage.get("status") != "approved":
            errors.append(f"reused_approved_artifacts: stage {stage['stage_id']} must have status approved.")
    reused_set = set(reused_ids)
    for stage in stages:
        if isinstance(stage, dict) and stage.get("status") == "approved" and stage.get("stage_id") not in reused_set:
            errors.append(f"{stage.get('stage_id')}: approved route status requires a reused_approved_artifacts entry.")
    return errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate an analysis route plan.")
    parser.add_argument("route_plan", type=Path)
    parser.add_argument("--require-executable", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    try:
        plan = load_json(args.route_plan)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Unable to read route plan: {exc}", file=sys.stderr)
        return 2
    if not isinstance(plan, dict):
        print("Route plan must be a JSON object.", file=sys.stderr)
        return 2

    errors, warnings = validate_route(plan, args.require_executable)
    report = {
        "schema_version": CURRENT_CONTRACT_VERSION,
        "valid": not errors,
        "executable": not errors and not plan.get("missing_inputs"),
        "route_sha256": sha256_json(plan),
        "checked_at": utc_now(),
        "errors": errors,
        "warnings": warnings,
    }
    if args.report:
        atomic_write_json(args.report, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
