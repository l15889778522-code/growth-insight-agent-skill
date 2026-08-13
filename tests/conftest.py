from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from render_stage_report import render_markdown
from contracts import CURRENT_CONTRACT_VERSION
from runctl import approve, initialize_run, load_state, record_agent_receipt, record_stage, set_route, start_stage
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
        "schema_version": CURRENT_CONTRACT_VERSION,
        "route_id": "route-test",
        "route_revision": revision,
        "task_type": "test",
        "stages": stages,
        "required_inputs": required,
        "provided_inputs": provided,
        "input_bindings": [
            {
                "input_name": name,
                "input_type": name,
                "artifact_id": f"fixture-{name}",
                "path": "request.json",
                "sha256": "0" * 64,
                "source": "request",
            }
            for name in provided
        ],
        "missing_inputs": sorted(set(required) - set(provided)),
        "fallback_route": None,
        "user_adjustments": [],
        "reused_approved_artifacts": reused or [],
        "created_at": utc_now(),
    }


def bind_route_inputs(run_dir: Path, route: dict[str, Any]) -> dict[str, Any]:
    bound = deepcopy(route)
    request = next(item for item in load_state(run_dir)["artifacts"] if item.get("kind") == "request")
    bound["input_bindings"] = [
        {
            "input_name": name,
            "input_type": name,
            "artifact_id": request["artifact_id"],
            "path": request["path"],
            "sha256": request["sha256"],
            "source": "request",
        }
        for name in bound["provided_inputs"]
    ]
    return bound


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
            "data_quality_warnings": [],
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
        "schema_version": CURRENT_CONTRACT_VERSION,
        "agent_contract_version": CURRENT_CONTRACT_VERSION,
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
    if output.get("schema_version") == CURRENT_CONTRACT_VERSION:
        state = load_state(run_dir)
        by_path = {
            item.get("path"): item
            for item in state["artifacts"]
            if item.get("path") and not item.get("superseded_by")
        }
        by_stage = {
            item.get("stage_id"): item
            for item in state["artifacts"]
            if item.get("stage_id") and item.get("artifact_id")
        }

        def normalize(reference: Any) -> Any:
            if not isinstance(reference, str):
                return reference
            path = reference.split("#", 1)[0]
            record = by_path.get(path)
            selector_type = "file"
            selector_value: Any = None
            if record is None and ":" in reference:
                stage_id, _, item_id = reference.partition(":")
                record = by_stage.get(stage_id)
                selector_type = "stage_field"
                selector_value = "" if not item_id else "/role_payload"
            if record is None:
                return reference
            return {
                "artifact_id": record["artifact_id"],
                "sha256": record["sha256"],
                "selector_type": selector_type,
                "selector_value": selector_value,
            }

        output["evidence"] = [normalize(item) for item in output.get("evidence", [])]
        for calculation in output.get("calculations", []):
            if isinstance(calculation, dict):
                calculation["input_evidence_refs"] = [
                    normalize(item) for item in calculation.get("input_evidence_refs", [])
                ]
        payload = output.get("role_payload", {})
        collections = []
        for key in ("observations", "recommendations", "findings"):
            collections.extend(payload.get(key, []))
        for item in collections:
            if isinstance(item, dict) and "evidence_refs" in item:
                item["evidence_refs"] = [normalize(reference) for reference in item["evidence_refs"]]

    attempt_dir = run_dir / "stages" / output["stage_id"] / f"attempt-{output['attempt']}"
    json_path = write_json(attempt_dir / "stage.json", output)
    raw_response_path = attempt_dir / "raw-response.txt"
    raw_response_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path = attempt_dir / "stage.md"
    markdown_path.write_text(render_markdown(output) + "\n", encoding="utf-8")
    report_path = write_json(
        attempt_dir / "validation.json",
        {
            "schema_version": CURRENT_CONTRACT_VERSION,
            "valid": True,
            "role": output["role"],
            "checked_at": utc_now(),
            "stage_sha256": sha256_json(output),
            "errors": [],
        },
    )
    runtime = load_state(run_dir)["stages"]
    active = next(item for item in runtime if item["stage_id"] == output["stage_id"])["runtime"]
    token_usage = active.get("token_usage") or {}
    agent_id = active.get("agent_id") or active.get("thread_id") or f"test-agent-{output['stage_id']}-{output['attempt']}"
    record_agent_receipt(
        run_dir,
        output["stage_id"],
        raw_response_path,
        json_path,
        agent_id=agent_id,
        model=active.get("model") or "gpt-test",
        input_tokens=token_usage.get("input_tokens", 1),
        output_tokens=token_usage.get("output_tokens", 1),
        capture_method="codex_tool_result",
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
        route = bind_route_inputs(run_dir, route_plan(roles, **route_kwargs))
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
