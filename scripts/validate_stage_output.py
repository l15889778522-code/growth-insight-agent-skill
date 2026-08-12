#!/usr/bin/env python3
"""Extract and validate one native subagent stage response."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from contracts import CURRENT_CONTRACT_VERSION, ROLES
from runtime_common import SCHEMA_DIR, atomic_write_json, load_json, schema_errors, sha256_json, utc_now



def extract_single_json(raw: str) -> dict[str, Any]:
    stripped = raw.strip()
    decoder = json.JSONDecoder()
    try:
        value, end = decoder.raw_decode(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Response is not a JSON object: {exc}") from exc
    if stripped[end:].strip():
        raise ValueError("Response contains content outside the single JSON object.")
    if not isinstance(value, dict):
        raise ValueError("Stage response must be a JSON object.")
    return value


def validate_stage(
    value: dict[str, Any],
    role: str,
    expected_run_id: str | None = None,
    expected_stage_id: str | None = None,
    expected_attempt: int | None = None,
    expected_contract_version: str | None = None,
) -> list[str]:
    if role not in ROLES:
        return [f"Unknown role: {role}"]
    errors = schema_errors(value, SCHEMA_DIR / "stages" / f"{role}.schema.json")
    expected = {
        "role": role,
        "run_id": expected_run_id,
        "stage_id": expected_stage_id,
        "attempt": expected_attempt,
        "schema_version": expected_contract_version,
        "agent_contract_version": expected_contract_version,
    }
    for field, wanted in expected.items():
        if wanted is not None and value.get(field) != wanted:
            errors.append(f"{field}: expected {wanted!r}, got {value.get(field)!r}")
    if value.get("stage_status") == "BLOCKED" and not value.get("required_next_inputs"):
        errors.append("required_next_inputs: BLOCKED output must explain what input is missing.")
    if value.get("conflicts") and value.get("stage_status") == "PASS":
        errors.append("stage_status: output with unresolved conflicts cannot be PASS.")
    if value.get("role") == "growth-review":
        payload = value.get("role_payload", {})
        decision = payload.get("decision")
        findings = payload.get("findings", [])
        severities = {item.get("severity") for item in findings if isinstance(item, dict)}
        finding_ids = [item.get("finding_id") for item in findings if isinstance(item, dict)]
        required_fixes = payload.get("required_fixes", [])
        lineage_breaks = payload.get("lineage_breaks", [])
        if decision and value.get("stage_status") != decision:
            errors.append("stage_status: Review stage_status must equal role_payload.decision.")
        if len(finding_ids) != len(set(finding_ids)):
            errors.append("role_payload.findings: finding_id values must be unique.")
        if (severities & {"P0", "P1"} or required_fixes) and decision != "FAIL":
            errors.append("role_payload.decision: P0/P1 findings or required fixes require FAIL.")
        if decision == "FAIL" and (not required_fixes or not payload.get("rollback_stage")):
            errors.append("role_payload: FAIL review requires required_fixes and rollback_stage.")
        if decision in {"PASS", "PASS_WITH_RISKS"} and payload.get("rollback_stage") is not None:
            errors.append("role_payload.rollback_stage: passing review must not request rollback.")
        if decision == "PASS_WITH_RISKS" and (severities & {"P0", "P1"} or required_fixes):
            errors.append("role_payload: PASS_WITH_RISKS may preserve only non-blocking findings.")
        if decision == "PASS_WITH_RISKS" and not value.get("risks"):
            errors.append("risks: PASS_WITH_RISKS review must list the risks preserved for Report.")
        if decision == "PASS" and (severities & {"P0", "P1", "P2"} or required_fixes or lineage_breaks):
            errors.append("role_payload: PASS requires no P0-P2 findings, required fixes, or lineage breaks.")
        if decision == "PASS" and value.get("risks"):
            errors.append("risks: PASS cannot preserve unresolved risks; use PASS_WITH_RISKS.")
        if any(not isinstance(item, str) for item in value.get("risks", [])):
            errors.append("risks: Review risks must be strings so Report caveat preservation is deterministic.")
    if value.get("role") == "growth-report" and value.get("recommended_next_stage") is not None:
        errors.append("recommended_next_stage: Report must terminate the route.")
    return errors


def validate_raw_response(
    raw: str,
    role: str,
    expected_run_id: str | None = None,
    expected_stage_id: str | None = None,
    expected_attempt: int | None = None,
    expected_contract_version: str | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        value = extract_single_json(raw)
    except ValueError as exc:
        return None, [str(exc)]
    return value, validate_stage(
        value,
        role,
        expected_run_id,
        expected_stage_id,
        expected_attempt,
        expected_contract_version,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a Codex native subagent stage response.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--raw-response", type=Path, help="Text file containing the complete subagent response.")
    source.add_argument("--json-file", type=Path, help="JSON file containing the stage object.")
    parser.add_argument("--role", required=True, choices=sorted(ROLES))
    parser.add_argument("--run-id")
    parser.add_argument("--stage-id")
    parser.add_argument("--attempt", type=int)
    parser.add_argument("--contract-version", default=CURRENT_CONTRACT_VERSION)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--validation-report", type=Path)
    args = parser.parse_args()

    if args.raw_response:
        raw = args.raw_response.read_text(encoding="utf-8")
        value, errors = validate_raw_response(
            raw,
            args.role,
            args.run_id,
            args.stage_id,
            args.attempt,
            args.contract_version,
        )
    else:
        try:
            loaded = load_json(args.json_file)
        except (OSError, json.JSONDecodeError) as exc:
            loaded = None
            errors = [f"Unable to read stage JSON: {exc}"]
        if isinstance(loaded, dict):
            value = loaded
            errors = validate_stage(
                value,
                args.role,
                args.run_id,
                args.stage_id,
                args.attempt,
                args.contract_version,
            )
        else:
            value = None
            errors = errors if "errors" in locals() else ["Stage JSON must contain an object."]

    report = {
        "schema_version": CURRENT_CONTRACT_VERSION,
        "valid": not errors,
        "role": args.role,
        "checked_at": utc_now(),
        "stage_sha256": sha256_json(value) if value is not None else None,
        "errors": errors,
    }
    if args.validation_report:
        atomic_write_json(args.validation_report, report)

    if errors:
        print("Stage output validation failed.", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 2

    if args.output_json and value is not None:
        atomic_write_json(args.output_json, value)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
