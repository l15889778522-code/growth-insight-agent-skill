from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from render_stage_report import render_markdown
from runctl import approve, initialize_run, load_state, record_stage, set_route, start_stage
from runtime_common import sha256_json, utc_now


def write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def route_plan(
    roles: list[str],
    *,
    revision: int = 1,
    required_inputs: list[str] | None = None,
    provided_inputs: list[str] | None = None,
    statuses: dict[str, str] | None = None,
    reused: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    required = required_inputs or ["business_question"]
    provided = provided_inputs or list(required)
    stages = []
    prior: str | None = None
    for sequence, role in enumerate(roles, start=1):
        stage_id = f"s{sequence:02d}-{role.removeprefix('growth-')}"
        stages.append(
            {
                "stage_id": stage_id,
                "role": role,
                "sequence": sequence,
                "depends_on": [prior] if prior else [],
                "required_inputs": [],
                "expected_outputs": [f"{role}.json"],
                "status": (statuses or {}).get(stage_id, "pending"),
            }
        )
        prior = stage_id
    return {
        "schema_version": "1.1",
        "route_id": "route-test",
        "route_revision": revision,
        "task_type": "test",
        "stages": stages,
        "required_inputs": required,
        "provided_inputs": provided,
        "missing_inputs": sorted(set(required) - set(provided)),
        "fallback_route": None,
        "user_adjustments": [],
        "reused_approved_artifacts": reused or [],
        "created_at": utc_now(),
    }


def metric(metric_id: str = "revenue_total", version: int = 1, formula: str = "SUM(revenue)") -> dict[str, Any]:
    return {
        "metric_id": metric_id,
        "name": "Revenue",
        "type": "core",
        "business_definition": "Booked revenue in the selected period.",
        "formula": formula,
        "grain": "day",
        "dimensions": ["day"],
        "time_window": "selected period",
        "field_dependencies": ["sales.revenue", "sales.day"],
        "status": "candidate",
        "source": "sales",
        "owner": "analytics",
        "data_risks": [],
        "version": version,
    }


def stage_output(
    role: str,
    run_id: str,
    stage_id: str,
    attempt: int,
    *,
    status: str = "PASS",
    sql: str = "SELECT day, SUM(revenue) AS revenue_total FROM sales GROUP BY day",
    source_file: str | None = None,
    source_sha256: str | None = None,
    metrics: list[dict[str, Any]] | None = None,
    review_risks: list[str] | None = None,
    evidence_ref: str = "data/result.csv",
) -> dict[str, Any]:
    metrics = metrics if metrics is not None else [metric()]
    common_metrics = [
        {"metric_id": item["metric_id"], "name": item["name"], "version": item["version"]}
        for item in metrics
    ] if role == "growth-metrics" else []
    payloads: dict[str, dict[str, Any]] = {
        "growth-business": {
            "business_context": "Test business context",
            "decision_question": "How is revenue changing?",
            "scope": ["daily revenue"],
            "non_goals": [],
            "dimensions": ["day"],
            "open_questions": [],
        },
        "growth-metrics": {
            "metrics": metrics,
            "dependency_gaps": [],
            "conflict_checks": ["No metric conflicts detected."],
        },
        "growth-sql": {
            "dialect": "sqlite",
            "field_mappings": [
                {
                    "metric_id": "revenue_total",
                    "source_fields": ["sales.revenue"],
                    "sql_expression": "SUM(revenue)",
                    "result_column": "revenue_total",
                    "supported": True,
                }
            ],
            "queries": [
                {
                    "sql_id": "q-revenue",
                    "purpose": "Calculate daily revenue",
                    "sql": sql,
                    "metric_ids": ["revenue_total"],
                    "risks": [],
                    "executable": True,
                }
            ],
            "unsupported_metrics": [],
        },
        "growth-insight": {
            "observations": [],
            "attribution_hypotheses": [],
            "counter_evidence": [],
            "validation_steps": ["Inspect the registered result."],
            "recommendations": [],
            "confidence_notes": [],
        },
        "growth-visualization": {
            "source_file": source_file,
            "source_sha256": source_sha256,
            "chart_specs": [
                {
                    "chart_id": "revenue-trend",
                    "title": "Revenue trend",
                    "type": "line",
                    "question": "How is revenue changing?",
                    "metric_id": "revenue_total",
                    "x": "day",
                    "y": "revenue_total",
                    "filters": [],
                    "aggregation": "none",
                    "output_formats": ["png", "html"],
                    "status": "ready" if source_file else "blocked_by_missing_data",
                }
            ],
            "reading_order": ["revenue-trend"],
        },
        "growth-review": {
            "decision": status if status in {"PASS", "PASS_WITH_RISKS", "FAIL"} else "FAIL",
            "findings": [],
            "required_fixes": ["Fix the earliest invalid stage."] if status == "FAIL" else [],
            "optional_improvements": [],
            "rollback_stage": "s01-business" if status == "FAIL" else None,
            "lineage_breaks": [],
        },
        "growth-report": {
            "executive_summary": "Revenue analysis completed.",
            "evidence_summary": ["See the registered query result."],
            "recommendations": [
                {
                    "recommendation_id": "rec-1",
                    "text": "Monitor daily revenue.",
                    "metric_ids": ["revenue_total"],
                    "evidence_refs": [evidence_ref],
                }
            ],
            "caveats": review_risks or [],
            "next_steps": ["Review the next period."],
        },
    }
    required_next = ["Provide the missing input."] if status == "BLOCKED" else []
    return {
        "schema_version": "1.1",
        "agent_contract_version": "1.1",
        "run_id": run_id,
        "stage_id": stage_id,
        "role": role,
        "attempt": attempt,
        "stage_status": status,
        "summary": f"{role} test output",
        "confirmed_decisions": [],
        "conflicts": [],
        "facts": [],
        "calculations": [],
        "assumptions": [],
        "hypotheses": [],
        "evidence": [],
        "metrics": common_metrics,
        "data_artifacts": [],
        "risks": review_risks or [],
        "open_questions": [],
        "required_next_inputs": required_next,
        "recommended_next_stage": None if role == "growth-report" else "next",
        "lineage": [],
        "role_payload": payloads[role],
    }


def materialize_stage(run_dir: Path, output: dict[str, Any]) -> dict[str, Any]:
    attempt_dir = run_dir / "stages" / output["stage_id"] / f"attempt-{output['attempt']}"
    json_path = write_json(attempt_dir / "stage.json", output)
    markdown_path = attempt_dir / "stage.md"
    markdown_path.write_text(render_markdown(output) + "\n", encoding="utf-8")
    report_path = write_json(
        attempt_dir / "validation.json",
        {
            "schema_version": "1.1",
            "valid": True,
            "role": output["role"],
            "checked_at": utc_now(),
            "stage_sha256": sha256_json(output),
            "errors": [],
        },
    )
    return record_stage(run_dir, json_path, markdown_path, report_path)


def approve_pending(run_dir: Path, approval_type: str, key: str) -> dict[str, Any]:
    state = load_state(run_dir)
    pending = state["pending_query"] if approval_type == "query" else state["pending_approval"]
    actions = {
        "route": "confirm_route",
        "stage": "approve_stage",
        "query": "execute_query",
        "rollback": "approve_rollback",
    }
    return approve(
        run_dir,
        approval_type,
        pending["subject_id"],
        pending["subject_revision"],
        pending["subject_sha256"],
        actions[approval_type],
        f"approve {approval_type}",
        key,
    )


@pytest.fixture
def initialized_run(tmp_path: Path):
    def factory(roles: list[str], **route_kwargs: Any) -> tuple[Path, dict[str, Any]]:
        run_dir = initialize_run(tmp_path / "runs", "test-run", {"question": "How is revenue changing?"})
        route = route_plan(roles, **route_kwargs)
        route_path = write_json(tmp_path / "route.json", route)
        set_route(run_dir, route_path)
        return run_dir, route

    return factory


@pytest.fixture
def approved_run(initialized_run):
    def factory(roles: list[str], **route_kwargs: Any) -> tuple[Path, dict[str, Any]]:
        run_dir, route = initialized_run(roles, **route_kwargs)
        approve_pending(run_dir, "route", "route-key-0001")
        return run_dir, route

    return factory
