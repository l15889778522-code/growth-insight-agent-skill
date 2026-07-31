#!/usr/bin/env python3
"""Aggregate repeatable evaluations for single Codex, v1.0, and v1.1 runs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import fmean
from typing import Any

from runtime_common import SCHEMA_DIR, atomic_write_json, atomic_write_text, load_json, sha256_file, utc_now, validate_schema


MODES = ("single-codex", "v1.0", "v1.1")


def _mean(values: list[float]) -> float | None:
    return round(fmean(values), 4) if values else None


def _evaluate_record(case: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    validate_schema(record, SCHEMA_DIR / "evaluation-record.schema.json")
    if record["case_id"] != case["case_id"]:
        raise ValueError(f"Evaluation record case_id mismatch for {case['case_id']}.")
    roles = record["roles_run"]
    required = set(case["required_roles"])
    forbidden = set(case["forbidden_roles"])
    route_correct = required.issubset(roles) and not (forbidden & set(roles)) and len(roles) <= case["max_roles"]
    checks = {"route_correct": route_correct, **record["rule_checks"]}
    return {
        "status": "completed" if record["completed"] else "incomplete",
        "case_id": case["case_id"],
        "mode": record["mode"],
        "rule_checks": checks,
        "rule_score": _mean([1.0 if value else 0.0 for value in checks.values()]),
        "quality_scores": record["quality_scores"],
        "quality_score": _mean(list(record["quality_scores"].values())),
        "hallucination_count": record["hallucination_count"],
        "privilege_violation_count": record["privilege_violation_count"],
        "gate_bypass_count": record["gate_bypass_count"],
        "human_revisions": record["human_revisions"],
        "elapsed_ms": record["elapsed_ms"],
        "token_usage": record["token_usage"],
        "roles_run": roles,
        "notes": record["notes"],
    }


def evaluate(cases_file: Path, results_root: Path) -> dict[str, Any]:
    suite = load_json(cases_file)
    cases = suite.get("cases", [])
    case_ids = [case.get("case_id") for case in cases]
    if suite.get("schema_version") != "1.1" or not cases or len(case_ids) != len(set(case_ids)):
        raise ValueError("Evaluation cases must use schema_version 1.1 and unique case IDs.")

    records: list[dict[str, Any]] = []
    for mode in MODES:
        for case in cases:
            path = results_root / mode / f"{case['case_id']}.json"
            if not path.is_file():
                records.append({"status": "not_run", "case_id": case["case_id"], "mode": mode})
                continue
            record = load_json(path)
            if record.get("mode") != mode:
                raise ValueError(f"Evaluation record mode mismatch: {path}")
            records.append(_evaluate_record(case, record))

    modes: dict[str, Any] = {}
    for mode in MODES:
        completed = [item for item in records if item["mode"] == mode and item["status"] == "completed"]
        elapsed = [item["elapsed_ms"] for item in completed if item["elapsed_ms"] is not None]
        tokens = [item["token_usage"]["total_tokens"] for item in completed if item["token_usage"] is not None]
        modes[mode] = {
            "completed_cases": len(completed),
            "total_cases": len(cases),
            "coverage": round(len(completed) / len(cases), 4),
            "rule_score": _mean([item["rule_score"] for item in completed]),
            "quality_score": _mean([item["quality_score"] for item in completed]),
            "hallucination_count": sum(item["hallucination_count"] for item in completed),
            "privilege_violation_count": sum(item["privilege_violation_count"] for item in completed),
            "gate_bypass_count": sum(item["gate_bypass_count"] for item in completed),
            "human_revisions": sum(item["human_revisions"] for item in completed),
            "mean_elapsed_ms": _mean([float(value) for value in elapsed]),
            "mean_total_tokens": _mean([float(value) for value in tokens]),
            "token_measurements": len(tokens),
        }
    return {
        "schema_version": "1.1",
        "generated_at": utc_now(),
        "cases_file": str(cases_file.resolve()),
        "cases_sha256": sha256_file(cases_file),
        "results_root": str(results_root.resolve()),
        "modes": modes,
        "records": records,
        "interpretation": "Compare quality only when mode coverage is equal; missing live runs are not scored as failures or successes.",
    }


def render_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Multi-Agent Data Analysis Evaluation",
        "",
        f"Generated: `{result['generated_at']}`",
        "",
        "| Mode | Coverage | Rule score | Quality score | Gate bypasses | Mean elapsed ms | Mean tokens |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for mode, item in result["modes"].items():
        lines.append(
            f"| {mode} | {item['completed_cases']}/{item['total_cases']} | {item['rule_score'] if item['rule_score'] is not None else 'N/A'} | "
            f"{item['quality_score'] if item['quality_score'] is not None else 'N/A'} | {item['gate_bypass_count']} | "
            f"{item['mean_elapsed_ms'] if item['mean_elapsed_ms'] is not None else 'N/A'} | {item['mean_total_tokens'] if item['mean_total_tokens'] is not None else 'N/A'} |"
        )
    lines.extend(["", result["interpretation"], "", "## Case Status", ""])
    for item in result["records"]:
        lines.append(f"- `{item['mode']}` / `{item['case_id']}`: {item['status']}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate the three-mode v1.1 evaluation suite.")
    parser.add_argument("--cases", type=Path, default=Path("tests/evals/cases.json"))
    parser.add_argument("--results-root", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-markdown", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(args.cases.resolve(), args.results_root.resolve())
    atomic_write_json(args.output_json, result)
    atomic_write_text(args.output_markdown, render_markdown(result))
    print(json.dumps(result["modes"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
