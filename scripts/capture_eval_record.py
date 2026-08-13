#!/usr/bin/env python3
"""Derive a v1.2 evaluation record from immutable workflow artifacts."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from evidence import resolve_evidence_reference
from runctl import audit_run
from runtime_common import (
    SCHEMA_DIR,
    atomic_write_json,
    load_json,
    read_jsonl,
    resolve_within,
    schema_errors,
    sha256_bytes,
    sha256_file,
    utc_now,
    validate_schema,
)
from sql_guard import validate_sql
from validate_route_plan import validate_route


QUALITY_DIMENSIONS = (
    "business_coverage",
    "metric_consistency",
    "route_quality",
    "sql_executability",
    "evidence_traceability",
)

ARTIFACT_SCHEMAS = {
    "query_request": "query-request.schema.json",
    "query_manifest": "query-manifest.schema.json",
    "result_profile": "result-profile.schema.json",
    "chart_specs": "chart-specs.schema.json",
    "final_report_manifest": "final-report-manifest.schema.json",
    "execution_lease": "execution-lease.schema.json",
}


def _relative(run_dir: Path, path: Path) -> str:
    return str(path.resolve().relative_to(run_dir.resolve())).replace("\\", "/")


def _source(run_dir: Path, path: Path) -> dict[str, str] | None:
    if not path.is_file():
        return None
    return {"path": _relative(run_dir, path), "sha256": sha256_file(path)}


def _sources(*items: dict[str, str] | None) -> list[dict[str, str]]:
    unique: dict[tuple[str, str], dict[str, str]] = {}
    for item in items:
        if item is not None:
            unique[(item["path"], item["sha256"])] = item
    return list(unique.values())


def _walk_evidence(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        if {"artifact_id", "sha256", "selector_type", "selector_value"}.issubset(value):
            yield value
        for child in value.values():
            yield from _walk_evidence(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_evidence(child)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _quality_review(path: Path | None) -> tuple[dict[str, Any] | None, dict[str, float] | None]:
    if path is None:
        return None, None
    review = load_json(path)
    if not isinstance(review, dict):
        raise ValueError("Quality review must contain a JSON object.")
    required = {"rubric_version", "blind_to_mode", "reviewer_ids", "scores", "rationales", "disagreements"}
    if set(review) != required:
        raise ValueError("Quality review fields do not match the v1.2 blind-review contract.")
    if review["blind_to_mode"] is not True:
        raise ValueError("Quality review must be blind to mode before adjudication.")
    scores = review.get("scores")
    rationales = review.get("rationales")
    if not isinstance(scores, dict) or set(scores) != set(QUALITY_DIMENSIONS):
        raise ValueError("Quality review scores must cover every rubric dimension.")
    if not isinstance(rationales, dict) or set(rationales) != set(QUALITY_DIMENSIONS):
        raise ValueError("Quality review rationales must cover every rubric dimension.")
    return review, scores


def _schema_checks(
    run_dir: Path,
    state: dict[str, Any],
    approvals: list[dict[str, Any]],
) -> tuple[list[str], list[dict[str, str]], list[tuple[dict[str, Any], dict[str, str]]]]:
    errors: list[str] = []
    sources: list[dict[str, str]] = []
    stage_outputs: list[tuple[dict[str, Any], dict[str, str]]] = []

    state_path = run_dir / "run-state.json"
    sources.extend(_sources(_source(run_dir, state_path)))
    errors.extend(f"run-state.json: {item}" for item in schema_errors(state, SCHEMA_DIR / "run-state.schema.json"))

    route_path = run_dir / "route-plan.json"
    if route_path.is_file():
        route = load_json(route_path)
        sources.extend(_sources(_source(run_dir, route_path)))
        route_errors, _ = validate_route(route, require_executable=False)
        errors.extend(f"route-plan.json: {item}" for item in route_errors)

    approvals_path = run_dir / "approvals.jsonl"
    sources.extend(_sources(_source(run_dir, approvals_path)))
    for index, approval in enumerate(approvals, start=1):
        errors.extend(
            f"approvals.jsonl[{index}]: {item}"
            for item in schema_errors(approval, SCHEMA_DIR / "approval.schema.json")
        )

    for stage in state.get("stages", []):
        artifact = stage.get("artifact") or {}
        relative = artifact.get("json_path")
        if not relative:
            continue
        try:
            path = resolve_within(run_dir, relative)
            output = load_json(path)
            source = _source(run_dir, path)
            if source is not None:
                sources.extend(_sources(source))
                stage_outputs.append((output, source))
            schema_path = SCHEMA_DIR / "stages" / f"{stage['role']}.schema.json"
            errors.extend(f"{relative}: {item}" for item in schema_errors(output, schema_path))
            receipt_path = artifact.get("receipt_path")
            if receipt_path:
                receipt_file = resolve_within(run_dir, receipt_path)
                receipt = load_json(receipt_file)
                sources.extend(_sources(_source(run_dir, receipt_file)))
                errors.extend(
                    f"{receipt_path}: {item}"
                    for item in schema_errors(receipt, SCHEMA_DIR / "agent-execution-receipt.schema.json")
                )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{relative}: {exc}")

    for lease in state.get("execution_leases", []):
        errors.extend(
            f"execution lease {lease.get('lease_id')}: {item}"
            for item in schema_errors(lease, SCHEMA_DIR / "execution-lease.schema.json")
        )

    for artifact in state.get("artifacts", []):
        kind = artifact.get("kind")
        relative = artifact.get("path")
        if not relative or kind not in {*ARTIFACT_SCHEMAS, "chart_manifest", "metric_lineage_latest"}:
            continue
        try:
            path = resolve_within(run_dir, relative)
            payload = load_json(path)
            sources.extend(_sources(_source(run_dir, path)))
            if kind == "chart_manifest":
                schema_name = "chart-render-manifest-v1.2.schema.json" if payload.get("schema_version") == "1.2" else "chart-render-manifest.schema.json"
            elif kind == "metric_lineage_latest":
                schema_name = "metric-lineage-v1.2.schema.json" if payload.get("schema_version") == "1.2" else "metric-lineage.schema.json"
            else:
                schema_name = ARTIFACT_SCHEMAS[kind]
            errors.extend(f"{relative}: {item}" for item in schema_errors(payload, SCHEMA_DIR / schema_name))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{relative}: {exc}")
    return errors, _sources(*sources), stage_outputs


def _sql_check(
    run_dir: Path,
    state: dict[str, Any],
    stage_outputs: list[tuple[dict[str, Any], dict[str, str]]],
) -> tuple[list[str], list[dict[str, str]]]:
    errors: list[str] = []
    sources: list[dict[str, str]] = []
    checked = 0
    for output, source in stage_outputs:
        if output.get("role") != "growth-sql":
            continue
        dialect = output.get("role_payload", {}).get("dialect", "mysql")
        for query in output.get("role_payload", {}).get("queries", []):
            if not query.get("executable"):
                continue
            checked += 1
            ok, query_errors = validate_sql(str(query.get("sql", "")), dialect=dialect, max_rows=1_000_000)
            if not ok:
                errors.extend(f"stage query {query.get('sql_id')}: {item}" for item in query_errors)
            sources.extend(_sources(source))
    requests: dict[tuple[str, int], dict[str, Any]] = {}
    for artifact in state.get("artifacts", []):
        if artifact.get("kind") != "query_request" or not artifact.get("path"):
            continue
        path = resolve_within(run_dir, artifact["path"])
        request = load_json(path)
        requests[(request["query_id"], request["revision"])] = request
        sources.extend(_sources(_source(run_dir, path)))
    for artifact in state.get("artifacts", []):
        if artifact.get("kind") != "query_sql" or not artifact.get("path"):
            continue
        metadata = artifact.get("metadata", {})
        request = requests.get((metadata.get("query_id"), metadata.get("query_revision")), {})
        path = resolve_within(run_dir, artifact["path"])
        checked += 1
        ok, query_errors = validate_sql(path.read_text(encoding="utf-8"), dialect=request.get("dialect", "mysql"), max_rows=request.get("max_rows", 1_000_000))
        if not ok:
            errors.extend(f"{artifact['path']}: {item}" for item in query_errors)
        sources.extend(_sources(_source(run_dir, path)))
    if checked == 0:
        errors.append("NOT_APPLICABLE: no executable SQL was produced.")
    return [item for item in errors if not item.startswith("NOT_APPLICABLE")], _sources(*sources)


def _evidence_check(
    run_dir: Path,
    state: dict[str, Any],
    stage_outputs: list[tuple[dict[str, Any], dict[str, str]]],
) -> tuple[list[str], list[dict[str, str]], int]:
    errors: list[str] = []
    sources: list[dict[str, str]] = []
    count = 0
    for output, source in stage_outputs:
        for reference in _walk_evidence(output):
            count += 1
            try:
                resolve_evidence_reference(run_dir, state, reference)
            except (OSError, ValueError, KeyError) as exc:
                errors.append(f"{source['path']}: {exc}")
            sources.extend(_sources(source))
            artifact = next(
                (item for item in state.get("artifacts", []) if item.get("artifact_id") == reference.get("artifact_id")),
                None,
            )
            if artifact:
                relative = artifact.get("path") or artifact.get("json_path")
                if relative:
                    sources.extend(_sources(_source(run_dir, resolve_within(run_dir, relative))))
    return errors, _sources(*sources), count


def _gate_check(
    run_dir: Path,
    state: dict[str, Any],
    approvals: list[dict[str, Any]],
    audit_errors: list[str],
) -> tuple[list[str], list[dict[str, str]]]:
    issues: list[str] = []
    sources = _sources(
        _source(run_dir, run_dir / "run-state.json"),
        _source(run_dir, run_dir / "events.jsonl"),
        _source(run_dir, run_dir / "approvals.jsonl"),
    )
    approvals_by_id = {item.get("approval_id"): item for item in approvals}
    if state.get("route_id") and not any(
        item.get("approval_type") == "route"
        and item.get("subject_id") == state.get("route_id")
        and item.get("subject_sha256") == state.get("route_sha256")
        for item in approvals
    ):
        issues.append("Current route has no matching recorded approval.")
    approved_by_stage = {item.get("stage_id"): item for item in state.get("approved_artifacts", [])}
    for stage in state.get("stages", []):
        artifact = stage.get("artifact") or {}
        if artifact and (not artifact.get("receipt_path") or not artifact.get("raw_response_path")):
            issues.append(f"Stage {stage['stage_id']} has an artifact without an Agent receipt and raw response.")
        if stage.get("status") not in {"approved", "completed"}:
            continue
        approved = approved_by_stage.get(stage["stage_id"])
        if not approved or approved.get("artifact_sha256") != artifact.get("sha256"):
            issues.append(f"Stage {stage['stage_id']} reached {stage['status']} without a matching approved artifact.")
        elif approved.get("approval_id") not in approvals_by_id:
            issues.append(f"Stage {stage['stage_id']} approval record is missing.")
    query_approval_hashes = {
        sha256_bytes(str(item.get("approval_id")).encode("utf-8"))
        for item in approvals
        if item.get("approval_type") == "query"
    }
    for lease in state.get("execution_leases", []):
        if lease.get("kind") == "query" and lease.get("state") == "completed":
            if lease.get("input_hashes", {}).get("query_approval") not in query_approval_hashes:
                issues.append(f"Completed query lease {lease.get('lease_id')} has no matching query approval.")
    for error in audit_errors:
        lowered = error.lower()
        if "approval" in lowered or "receipt" in lowered or "agent" in lowered:
            issues.append(f"Audit: {error}")
    return list(dict.fromkeys(issues)), sources


def _token_usage(state: dict[str, Any]) -> tuple[dict[str, int] | None, int]:
    usages: list[dict[str, Any] | None] = []
    for stage in state.get("stages", []):
        runtimes = list(stage.get("attempt_history", []))
        if stage.get("runtime"):
            runtimes.append(stage["runtime"])
        usages.extend(runtime.get("token_usage") for runtime in runtimes)
    if not usages or any(
        not isinstance(usage, dict)
        or not all(isinstance(usage.get(key), int) for key in ("input_tokens", "output_tokens", "total_tokens"))
        for usage in usages
    ):
        return None, sum(1 for usage in usages if usage is None)
    return {
        key: sum(int(usage[key]) for usage in usages if usage is not None)
        for key in ("input_tokens", "output_tokens", "total_tokens")
    }, 0


def derive_record(
    run_dir: Path,
    case: dict[str, Any],
    *,
    mode: str = "controlled-skill",
    quality_review_path: Path | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    state_path = run_dir / "run-state.json"
    events_path = run_dir / "events.jsonl"
    state = load_json(state_path)
    if state.get("schema_version") != "1.2":
        raise ValueError("Artifact-derived evaluation requires a migrated v1.2 run.")
    events = read_jsonl(events_path)
    approvals = read_jsonl(run_dir / "approvals.jsonl")
    audit_errors = audit_run(run_dir)
    schema_failures, schema_sources, stage_outputs = _schema_checks(run_dir, state, approvals)
    sql_failures, sql_sources = _sql_check(run_dir, state, stage_outputs)
    evidence_failures, evidence_sources, evidence_count = _evidence_check(run_dir, state, stage_outputs)
    gate_failures, gate_sources = _gate_check(run_dir, state, approvals, audit_errors)
    recovery_events = [
        item for item in events if item.get("event_type") in {"state_recovered", "execution_leases_recovered"}
    ]
    active_leases = [
        item for item in state.get("execution_leases", []) if item.get("state") in {"prepared", "executing", "publishing"}
    ]
    recovery_required = bool(case.get("recovery_required"))
    recovery_ok = not active_leases and (bool(recovery_events) if recovery_required else True)
    recovery_details = []
    if recovery_required and not recovery_events:
        recovery_details.append("Case requires recovery, but no committed recovery event exists.")
    if active_leases:
        recovery_details.append("Run still contains active execution leases.")

    quality_review, quality_scores = _quality_review(quality_review_path)
    roles_run = list(
        dict.fromkeys(
            stage["role"]
            for stage in state.get("stages", [])
            if stage.get("attempt", 0) > 0 or stage.get("artifact")
        )
    )
    started = _parse_time(state.get("created_at"))
    finished = _parse_time(state.get("updated_at"))
    elapsed_ms = max(0, int((finished - started).total_seconds() * 1000)) if started and finished else None
    token_usage, missing_token_records = _token_usage(state)
    revision_events = {"revision_requested", "metric_edit_requested"}
    human_revisions = sum(1 for item in events if item.get("event_type") in revision_events)
    human_revisions += max(0, sum(1 for item in events if item.get("event_type") == "route_proposed") - 1)

    rule_checks = {
        "sql_safe": not sql_failures,
        "schema_valid": not schema_failures,
        "evidence_resolvable": not evidence_failures,
        "no_gate_bypass": not gate_failures,
        "recovery_success": recovery_ok,
    }
    rule_evidence = {
        "sql_safe": {"passed": rule_checks["sql_safe"], "sources": sql_sources, "details": sql_failures or ["All executable SQL passed the AST read-only guard, or SQL was not applicable."]},
        "schema_valid": {"passed": rule_checks["schema_valid"], "sources": schema_sources, "details": schema_failures or ["All discovered v1.2 artifacts satisfy their schemas."]},
        "evidence_resolvable": {"passed": rule_checks["evidence_resolvable"], "sources": evidence_sources, "details": evidence_failures or [f"Resolved {evidence_count} structured evidence reference(s)."]},
        "no_gate_bypass": {"passed": rule_checks["no_gate_bypass"], "sources": gate_sources, "details": gate_failures or ["Route, stage, query, and Agent receipt gates are consistent with saved artifacts."]},
        "recovery_success": {"passed": recovery_ok, "sources": _sources(_source(run_dir, events_path), _source(run_dir, state_path)), "details": recovery_details or ["No recovery was required, or committed recovery completed without an active lease."]},
    }
    record_notes = list(notes or [])
    if quality_scores is None:
        record_notes.append("Quality score not measured: no blind-review record was supplied.")
    if missing_token_records or token_usage is None:
        record_notes.append("Token usage is unavailable or incomplete in one or more Agent receipts.")
    if audit_errors:
        record_notes.append(f"Run audit reported {len(audit_errors)} error(s); see provenance.audit_errors.")
    record = {
        "schema_version": "1.2",
        "case_id": case["case_id"],
        "mode": mode,
        "completed": state.get("status") == "completed" and not audit_errors,
        "roles_run": roles_run,
        "rule_checks": rule_checks,
        "rule_evidence": rule_evidence,
        "provenance": {
            "source_type": "run_artifacts",
            "run_id": state.get("run_id"),
            "run_state_sha256": sha256_file(state_path),
            "events_sha256": sha256_file(events_path),
            "captured_at": utc_now(),
            "audit_errors": audit_errors,
        },
        "quality_scores": quality_scores,
        "quality_review": quality_review,
        "hallucination_count": None,
        "privilege_violation_count": None,
        "gate_bypass_count": len(gate_failures),
        "human_revisions": human_revisions,
        "elapsed_ms": elapsed_ms,
        "token_usage": token_usage,
        "notes": list(dict.fromkeys(record_notes)),
    }
    validate_schema(record, SCHEMA_DIR / "evaluation-record.schema.json")
    return record


def _case(cases_file: Path, case_id: str) -> dict[str, Any]:
    suite = load_json(cases_file)
    if suite.get("schema_version") != "1.2":
        raise ValueError("Evaluation cases must use schema_version 1.2.")
    matches = [item for item in suite.get("cases", []) if item.get("case_id") == case_id]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one evaluation case named {case_id!r}.")
    return matches[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="Derive a v1.2 evaluation record from a saved run.")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--cases", type=Path, default=Path("tests/evals/cases.json"))
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--mode", choices=("controlled-skill",), default="controlled-skill")
    parser.add_argument("--quality-review", type=Path)
    parser.add_argument("--note", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    record = derive_record(
        args.run_dir,
        _case(args.cases, args.case_id),
        mode=args.mode,
        quality_review_path=args.quality_review,
        notes=args.note,
    )
    atomic_write_json(args.output, record)
    print(json.dumps({"output": str(args.output.resolve()), "completed": record["completed"], "rule_checks": record["rule_checks"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
