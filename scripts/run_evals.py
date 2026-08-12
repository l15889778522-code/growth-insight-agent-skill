#!/usr/bin/env python3
"""Aggregate repeatable evaluations for single Codex, v1.0, and v1.2 runs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import fmean, pstdev
from typing import Any

from runtime_common import SCHEMA_DIR, atomic_write_json, atomic_write_text, load_json, sha256_file, utc_now, validate_schema


MODES = ("single-codex", "v1.0", "v1.2")


def _mean(values: list[float]) -> float | None:
    return round(fmean(values), 4) if values else None


def _variation(values: list[float]) -> float | None:
    return round(pstdev(values), 4) if len(values) > 1 else None


def _evaluate_record(case: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    validate_schema(record, SCHEMA_DIR / "evaluation-record.schema.json")
    if record["case_id"] != case["case_id"]:
        raise ValueError(f"Evaluation record case_id mismatch for {case['case_id']}.")
    if record.get("schema_version") == "1.2":
        for name, passed in record["rule_checks"].items():
            if record["rule_evidence"][name]["passed"] is not passed:
                raise ValueError(f"Evaluation rule evidence disagrees with rule_checks.{name}.")
        review = record.get("quality_review")
        if (review is None) != (record.get("quality_scores") is None):
            raise ValueError("Evaluation quality_scores and quality_review must either both be present or both be null.")
        if review is not None and review["scores"] != record["quality_scores"]:
            raise ValueError("Evaluation quality_scores differ from the saved blind-review adjudication.")
    roles = record["roles_run"]
    required = set(case["required_roles"])
    forbidden = set(case["forbidden_roles"])
    route_correct = required.issubset(roles) and not (forbidden & set(roles)) and len(roles) <= case["max_roles"]
    checks = {"route_correct": route_correct, **record["rule_checks"]}
    quality_scores = record["quality_scores"]
    return {
        "status": "completed" if record["completed"] else "incomplete",
        "case_id": case["case_id"],
        "mode": record["mode"],
        "rule_checks": checks,
        "rule_score": _mean([1.0 if value else 0.0 for value in checks.values()]),
        "quality_scores": quality_scores,
        "quality_score": _mean(list(quality_scores.values())) if quality_scores is not None else None,
        "hallucination_count": record["hallucination_count"],
        "privilege_violation_count": record["privilege_violation_count"],
        "gate_bypass_count": record["gate_bypass_count"],
        "human_revisions": record["human_revisions"],
        "elapsed_ms": record["elapsed_ms"],
        "token_usage": record["token_usage"],
        "roles_run": roles,
        "provenance": record.get("provenance"),
        "rule_evidence": record.get("rule_evidence"),
        "notes": record["notes"],
    }


def _record_paths(results_root: Path, mode: str, case_id: str) -> list[Path]:
    flat = results_root / mode / f"{case_id}.json"
    repeated = results_root / mode / case_id
    paths = [flat] if flat.is_file() else []
    if repeated.is_dir():
        paths.extend(sorted(repeated.glob("*.json")))
    return paths


def evaluate(cases_file: Path, results_root: Path) -> dict[str, Any]:
    suite = load_json(cases_file)
    cases = suite.get("cases", [])
    case_ids = [case.get("case_id") for case in cases]
    if suite.get("schema_version") != "1.2" or not cases or len(case_ids) != len(set(case_ids)):
        raise ValueError("Evaluation cases must use schema_version 1.2 and unique case IDs.")

    records: list[dict[str, Any]] = []
    for mode in MODES:
        for case in cases:
            paths = _record_paths(results_root, mode, case["case_id"])
            if not paths:
                records.append({"status": "not_run", "case_id": case["case_id"], "mode": mode})
                continue
            for path in paths:
                record = load_json(path)
                if record.get("mode") != mode:
                    raise ValueError(f"Evaluation record mode mismatch: {path}")
                evaluated = _evaluate_record(case, record)
                evaluated["record_path"] = str(path.resolve())
                evaluated["record_sha256"] = sha256_file(path)
                records.append(evaluated)

    modes: dict[str, Any] = {}
    for mode in MODES:
        completed = [item for item in records if item["mode"] == mode and item["status"] == "completed"]
        attempted = [item for item in records if item["mode"] == mode and item["status"] != "not_run"]
        elapsed = [float(item["elapsed_ms"]) for item in completed if item["elapsed_ms"] is not None]
        tokens = [float(item["token_usage"]["total_tokens"]) for item in completed if item["token_usage"] is not None]
        rule_scores = [float(item["rule_score"]) for item in completed if item["rule_score"] is not None]
        quality_scores = [float(item["quality_score"]) for item in completed if item["quality_score"] is not None]
        completed_case_ids = {item["case_id"] for item in completed}
        hallucinations = [item["hallucination_count"] for item in completed if item["hallucination_count"] is not None]
        privilege_violations = [item["privilege_violation_count"] for item in completed if item["privilege_violation_count"] is not None]
        modes[mode] = {
            "completed_cases": len(completed_case_ids),
            "total_cases": len(cases),
            "coverage": round(len(completed_case_ids) / len(cases), 4),
            "attempted_runs": len(attempted),
            "completed_runs": len(completed),
            "success_rate": round(len(completed) / len(attempted), 4) if attempted else None,
            "rule_score": _mean(rule_scores),
            "rule_score_stddev": _variation(rule_scores),
            "quality_score": _mean(quality_scores),
            "quality_score_stddev": _variation(quality_scores),
            "quality_measurements": len(quality_scores),
            "hallucination_count": sum(hallucinations) if hallucinations else None,
            "hallucination_measurements": len(hallucinations),
            "privilege_violation_count": sum(privilege_violations) if privilege_violations else None,
            "privilege_violation_measurements": len(privilege_violations),
            "gate_bypass_count": sum(item["gate_bypass_count"] for item in completed),
            "human_revisions": sum(item["human_revisions"] for item in completed),
            "mean_elapsed_ms": _mean(elapsed),
            "elapsed_ms_stddev": _variation(elapsed),
            "mean_total_tokens": _mean(tokens),
            "total_tokens_stddev": _variation(tokens),
            "token_measurements": len(tokens),
        }
    return {
        "schema_version": "1.2",
        "generated_at": utc_now(),
        "cases_file": str(cases_file.resolve()),
        "cases_sha256": sha256_file(cases_file),
        "results_root": str(results_root.resolve()),
        "modes": modes,
        "records": records,
        "interpretation": "Compare quality and cost only at equal case and measurement coverage; missing live runs and unmeasured blind-review or Token fields are not scored as failures or successes.",
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
    parser = argparse.ArgumentParser(description="Aggregate the three-mode v1.2 evaluation suite.")
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
