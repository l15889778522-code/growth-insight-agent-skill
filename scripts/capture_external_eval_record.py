#!/usr/bin/env python3
"""Capture a Schema-valid evaluation record from saved external-mode evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from runtime_common import SCHEMA_DIR, atomic_write_json, load_json, sha256_file, utc_now, validate_schema


MODES = ("single-codex", "codex-native-free")
RULES = ("sql_safe", "schema_valid", "evidence_resolvable", "no_gate_bypass", "recovery_success")
REPO_ROOT = Path(__file__).resolve().parent.parent


def _portable_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(resolved)


def capture(manifest_path: Path, evidence_dir: Path) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    mode = manifest.get("mode")
    if mode not in MODES:
        raise ValueError(f"External evaluation mode must be one of {MODES}.")
    rule_checks = manifest.get("rule_checks")
    if not isinstance(rule_checks, dict) or set(rule_checks) != set(RULES):
        raise ValueError(f"rule_checks must contain exactly {RULES}.")

    evidence_files = {path.resolve() for path in evidence_dir.rglob("*") if path.is_file()}
    evidence_files.add(manifest_path.resolve())
    if not evidence_files:
        raise ValueError("External evaluation evidence directory is empty.")
    sources = [
        {"path": _portable_path(path), "sha256": sha256_file(path)}
        for path in sorted(evidence_files, key=lambda item: _portable_path(item))
    ]
    details = manifest.get("rule_details", {})
    rule_evidence = {
        name: {
            "passed": rule_checks[name],
            "sources": sources,
            "details": [str(item) for item in details.get(name, ["Checked against the saved external evidence bundle."])],
        }
        for name in RULES
    }
    record = {
        "schema_version": "1.2",
        "case_id": manifest["case_id"],
        "mode": mode,
        "completed": bool(manifest["completed"]),
        "roles_run": manifest["roles_run"],
        "rule_checks": {name: rule_checks[name] for name in RULES},
        "rule_evidence": rule_evidence,
        "provenance": {
            "source_type": "external_adapter",
            "run_id": None,
            "run_state_sha256": None,
            "events_sha256": None,
            "captured_at": utc_now(),
            "audit_errors": [],
        },
        "quality_scores": None,
        "quality_review": None,
        "hallucination_count": manifest.get("hallucination_count"),
        "privilege_violation_count": manifest.get("privilege_violation_count"),
        "gate_bypass_count": manifest.get("gate_bypass_count"),
        "human_revisions": int(manifest.get("human_revisions", 0)),
        "elapsed_ms": manifest.get("elapsed_ms"),
        "token_usage": manifest.get("token_usage"),
        "notes": [str(item) for item in manifest.get("notes", [])],
    }
    validate_schema(record, SCHEMA_DIR / "evaluation-record.schema.json")
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture one external-mode evaluation record.")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    record = capture(args.manifest.resolve(), args.evidence_dir.resolve())
    atomic_write_json(args.output, record)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
