from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import runctl
from conftest import (
    ROOT,
    approve_pending,
    bind_route_inputs,
    materialize_stage,
    metric,
    route_plan,
    stage_output,
    write_json,
)
from contracts import CURRENT_CONTRACT_VERSION
from render_stage_report import render_markdown
from runctl import (
    approve,
    audit_run,
    begin_execution_lease,
    initialize_run,
    load_state,
    migrate_run,
    prepare_query,
    record_agent_receipt,
    record_stage,
    register_artifacts,
    set_route,
    start_stage,
    stop,
)
from runtime_common import sha256_bytes, sha256_json, utc_now
from validate_stage_output import validate_stage


def _stage_files(run_dir: Path, output: dict) -> tuple[Path, Path, Path, Path]:
    attempt_dir = run_dir / "stages" / output["stage_id"] / f"attempt-{output['attempt']}"
    stage_path = write_json(attempt_dir / "stage.json", output)
    raw_path = attempt_dir / "raw-response.txt"
    raw_path.write_text(json.dumps(output, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path = attempt_dir / "stage.md"
    markdown_path.write_text(render_markdown(output) + "\n", encoding="utf-8")
    validation_path = write_json(
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
    return raw_path, stage_path, markdown_path, validation_path


def test_v11_golden_run_audits_and_migrates(tmp_path: Path) -> None:
    source = ROOT / "tests" / "fixtures" / "v1.1-golden-run" / "v11-golden"
    run_dir = tmp_path / "v11-golden"
    shutil.copytree(source, run_dir)

    assert audit_run(run_dir) == []
    migrated = migrate_run(run_dir)

    assert migrated["schema_version"] == CURRENT_CONTRACT_VERSION
    assert migrated["runtime"]["contract_version"] == CURRENT_CONTRACT_VERSION
    assert any(item["kind"] == "request" and item.get("artifact_id") for item in migrated["artifacts"])
    assert audit_run(run_dir) == []


def test_event_payload_tampering_breaks_the_v12_chain(tmp_path: Path) -> None:
    run_dir = initialize_run(tmp_path / "runs", "payload-chain", {"question": "x"})
    events_path = run_dir / "events.jsonl"
    event = json.loads(events_path.read_text(encoding="utf-8"))
    event["payload"]["request_semantic_sha256"] = "0" * 64
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    assert any("content hash" in error for error in audit_run(run_dir))


def test_approval_provenance_is_self_asserted_and_hash_bound(initialized_run) -> None:
    run_dir, _ = initialized_run(["growth-business"])
    pending = load_state(run_dir)["pending_approval"]
    approval = approve(
        run_dir,
        "route",
        pending["subject_id"],
        pending["subject_revision"],
        pending["subject_sha256"],
        "confirm_route",
        "I confirm this route",
        "provenance-key",
        source_surface="codex-desktop",
    )

    assert approval["provenance"]["trust_level"] == "self_asserted"
    assert approval["provenance"]["source_message_sha256"] == sha256_bytes(b"I confirm this route")
    with pytest.raises(ValueError, match="different approval"):
        approve(
            run_dir,
            "route",
            pending["subject_id"],
            pending["subject_revision"],
            pending["subject_sha256"],
            "confirm_route",
            "I confirm this route",
            "provenance-key",
            source_surface="forged-surface",
        )


def test_route_binding_must_resolve_to_a_registered_artifact(tmp_path: Path) -> None:
    run_dir = initialize_run(tmp_path / "runs", "route-binding", {"question": "x"})
    route = route_plan(["growth-business"])
    with pytest.raises(ValueError, match="artifact_id"):
        set_route(run_dir, write_json(tmp_path / "route.json", route))


def test_registered_artifact_path_cannot_be_overwritten(tmp_path: Path) -> None:
    run_dir = initialize_run(tmp_path / "runs", "immutable-artifact", {"question": "x"})
    path = run_dir / "data" / "evidence.csv"
    path.write_text("value\n1\n", encoding="utf-8")
    register_artifacts(run_dir, [{"kind": "user_result", "path": path}])
    path.write_text("value\n2\n", encoding="utf-8")

    with pytest.raises(ValueError, match="immutable"):
        register_artifacts(run_dir, [{"kind": "user_result", "path": path}])
    assert any("Artifact hash mismatch" in error for error in audit_run(run_dir))


def test_stop_aborts_a_query_execution_lease(approved_run, tmp_path: Path) -> None:
    sql = "SELECT day, SUM(revenue) AS revenue_total FROM sales GROUP BY day"
    run_dir, _ = approved_run(["growth-metrics", "growth-sql", "growth-review", "growth-report"])
    start_stage(run_dir, "s01-metrics")
    materialize_stage(run_dir, stage_output("growth-metrics", "test-run", "s01-metrics", 1))
    approve_pending(run_dir, "stage", "lease-metrics-key")
    start_stage(run_dir, "s02-sql")
    materialize_stage(run_dir, stage_output("growth-sql", "test-run", "s02-sql", 1, sql=sql))
    approve_pending(run_dir, "stage", "lease-sql-key")
    sql_path = tmp_path / "query.sql"
    sql_path.write_text(sql, encoding="utf-8")
    request = prepare_query(
        run_dir,
        sql_path,
        "q-revenue",
        "source-test",
        "sqlite",
        10,
        100,
        4096,
        "a" * 64,
    )
    approve_pending(run_dir, "query", "lease-query-key")
    state = load_state(run_dir)
    input_ids = [
        item["artifact_id"]
        for item in state["artifacts"]
        if item.get("kind") in {"query_request", "query_sql"}
        and item.get("metadata", {}).get("query_revision") == request["revision"]
    ]
    lease = begin_execution_lease(
        run_dir,
        "query",
        request["query_id"],
        request["revision"],
        input_artifact_ids=input_ids,
    )

    stopped = stop(run_dir, "cancel query")
    saved = next(item for item in stopped["execution_leases"] if item["lease_id"] == lease["lease_id"])
    assert saved["state"] == "aborted"
    assert saved["abort_reason"] == "run_stopped"


def test_record_stage_requires_an_agent_execution_receipt(approved_run) -> None:
    run_dir, _ = approved_run(["growth-business"])
    start_stage(run_dir, "s01-business")
    output = stage_output("growth-business", "test-run", "s01-business", 1)
    _, stage_path, markdown_path, validation_path = _stage_files(run_dir, output)

    with pytest.raises(ValueError, match="no Agent execution receipt"):
        record_stage(run_dir, stage_path, markdown_path, validation_path)


def test_record_stage_marks_malformed_common_fields_failed_without_runtime_crash(approved_run) -> None:
    run_dir, _ = approved_run(["growth-business"])
    start_stage(run_dir, "s01-business")
    output = stage_output("growth-business", "test-run", "s01-business", 1)
    output["data_artifacts"] = ["request.json"]
    _, stage_path, markdown_path, validation_path = _stage_files(run_dir, output)

    with pytest.raises(ValueError, match="Stage output is invalid") as error:
        record_stage(run_dir, stage_path, markdown_path, validation_path)

    assert "data_artifacts" in str(error.value)
    state = load_state(run_dir)
    assert state["status"] == "failed"
    assert state["stages"][0]["runtime"]["failure_class"] == "stage_validation_failed"


def test_v12_confirmed_decisions_reject_wrapper_objects() -> None:
    output = stage_output("growth-business", "test-run", "s01-business", 1)
    output["confirmed_decisions"] = [{"decision": "Use booked revenue."}]

    errors = validate_stage(output, "growth-business", "test-run", "s01-business", 1)

    assert any("confirmed_decisions" in error for error in errors)


def test_sql_stage_must_account_for_every_approved_metric(approved_run) -> None:
    run_dir, _ = approved_run(["growth-metrics", "growth-sql"])
    start_stage(run_dir, "s01-metrics")
    metrics = [metric(), metric("order_count", formula="COUNT(order_id)")]
    materialize_stage(run_dir, stage_output("growth-metrics", "test-run", "s01-metrics", 1, metrics=metrics))
    approve_pending(run_dir, "stage", "approve-two-metrics")
    start_stage(run_dir, "s02-sql")
    sql_output = stage_output("growth-sql", "test-run", "s02-sql", 1)

    with pytest.raises(ValueError, match="did not map or explicitly mark unsupported metric IDs: order_count"):
        materialize_stage(run_dir, sql_output)


def test_agent_receipt_is_single_use_and_binds_raw_response(approved_run) -> None:
    run_dir, _ = approved_run(["growth-business"])
    start_stage(run_dir, "s01-business")
    output = stage_output("growth-business", "test-run", "s01-business", 1)
    raw_path, stage_path, markdown_path, validation_path = _stage_files(run_dir, output)
    record_agent_receipt(
        run_dir,
        "s01-business",
        raw_path,
        stage_path,
        agent_id="agent-native-1",
        model="gpt-test",
        input_tokens=10,
        output_tokens=5,
        capture_method="codex_tool_result",
    )
    with pytest.raises(ValueError, match="already has"):
        record_agent_receipt(run_dir, "s01-business", raw_path, stage_path)

    raw_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Raw Agent response"):
        record_stage(run_dir, stage_path, markdown_path, validation_path)


def test_v12_run_rejects_a_legacy_agent_response(approved_run) -> None:
    run_dir, _ = approved_run(["growth-business"])
    start_stage(run_dir, "s01-business")
    output = stage_output("growth-business", "test-run", "s01-business", 1)
    output["schema_version"] = "1.1"
    output["agent_contract_version"] = "1.1"
    raw_path, stage_path, _, _ = _stage_files(run_dir, output)

    with pytest.raises(ValueError, match="schema_version: expected '1.2'"):
        record_agent_receipt(run_dir, "s01-business", raw_path, stage_path)


def test_record_stage_accepts_paths_relative_to_the_run_directory(approved_run) -> None:
    run_dir, _ = approved_run(["growth-business"])
    start_stage(run_dir, "s01-business")
    output = stage_output("growth-business", "test-run", "s01-business", 1)
    raw_path, stage_path, markdown_path, validation_path = _stage_files(run_dir, output)
    record_agent_receipt(
        run_dir,
        "s01-business",
        raw_path.relative_to(run_dir),
        stage_path.relative_to(run_dir),
    )

    state = record_stage(
        run_dir,
        stage_path.relative_to(run_dir),
        markdown_path.relative_to(run_dir),
        validation_path.relative_to(run_dir),
    )

    assert state["status"] == "awaiting_user_confirmation"


def test_agent_config_change_blocks_receipt(approved_run, monkeypatch) -> None:
    run_dir, _ = approved_run(["growth-business"])
    start_stage(run_dir, "s01-business")
    output = stage_output("growth-business", "test-run", "s01-business", 1)
    raw_path, stage_path, _, _ = _stage_files(run_dir, output)
    original = runctl._agent_settings

    def changed_settings():
        hashes, models, settings = original()
        hashes["growth-business"] = "0" * 64
        return hashes, models, settings

    monkeypatch.setattr(runctl, "_agent_settings", changed_settings)
    with pytest.raises(ValueError, match="configuration"):
        record_agent_receipt(run_dir, "s01-business", raw_path, stage_path)


def test_structured_evidence_rejects_a_missing_csv_cell(approved_run) -> None:
    run_dir, _ = approved_run(["growth-insight"])
    result = run_dir / "data" / "result.csv"
    result.write_text("day,revenue_total\n2026-01-01,10\n", encoding="utf-8")
    record = register_artifacts(run_dir, [{"kind": "user_result", "path": result}])[0]
    start_stage(run_dir, "s01-insight")
    output = stage_output("growth-insight", "test-run", "s01-insight", 1)
    output["role_payload"]["observations"] = [
        {
            "observation_id": "obs-1",
            "statement": "Revenue is present.",
            "metric_ids": ["revenue_total"],
            "evidence_refs": [
                {
                    "artifact_id": record["artifact_id"],
                    "sha256": record["sha256"],
                    "selector_type": "csv_cell",
                    "selector_value": {"row": 99, "column": "revenue_total"},
                }
            ],
            "confidence": "high",
        }
    ]
    with pytest.raises(ValueError, match="row does not exist"):
        materialize_stage(run_dir, output)


@pytest.mark.parametrize(
    ("decision", "severity", "required_fixes", "expected"),
    [
        ("PASS", "P1", [], "P0/P1"),
        ("PASS", "P2", [], "PASS requires"),
        ("PASS_WITH_RISKS", "P1", [], "P0/P1"),
        ("PASS_WITH_RISKS", "P3", ["must fix"], "required fixes"),
    ],
)
def test_review_severity_matrix_is_machine_enforced(
    decision: str,
    severity: str,
    required_fixes: list[str],
    expected: str,
) -> None:
    value = stage_output(
        "growth-review",
        "test-run",
        "s01-review",
        1,
        status=decision,
        review_risks=["preserved risk"] if decision == "PASS_WITH_RISKS" else [],
    )
    value["role_payload"]["findings"] = [
        {
            "finding_id": "finding-1",
            "severity": severity,
            "summary": "A review finding.",
            "evidence_refs": ["legacy:reference"],
            "responsible_stage": None,
        }
    ]
    value["role_payload"]["required_fixes"] = required_fixes
    errors = validate_stage(value, "growth-review")
    assert any(expected in error for error in errors)


@pytest.mark.parametrize("copy_warning", [False, True])
def test_review_must_copy_latest_query_quality_warnings_exactly(
    approved_run,
    copy_warning: bool,
) -> None:
    run_dir, _ = approved_run(["growth-review"])
    warning = "Query returned fewer than 30 rows; Review must assess sample-size risk."
    manifest = {
        "schema_version": "1.2",
        "query_id": "q-quality",
        "data_source_type": "sqlite",
        "data_source_id": "source-test",
        "data_source_fingerprint": "a" * 64,
        "sql_sha256": "b" * 64,
        "dialect": "sqlite",
        "started_at": utc_now(),
        "completed_at": utc_now(),
        "elapsed_ms": 1,
        "timeout_seconds": 10,
        "max_rows": 100,
        "max_result_bytes": 4096,
        "returned_rows": 2,
        "columns": [],
        "result_path": "data/result.csv",
        "result_bytes": 4,
        "result_sha256": "c" * 64,
        "profile_path": "data/result-profile.json",
        "profile_sha256": "d" * 64,
        "truncated": False,
        "sampled": False,
        "quality_warnings": [warning],
    }
    path = write_json(run_dir / "data" / "query-manifest.json", manifest)
    record = register_artifacts(
        run_dir,
        [
            {
                "kind": "query_manifest",
                "path": path,
                "metadata": {"query_id": "q-quality", "query_revision": 1},
            }
        ],
    )[0]
    start_stage(run_dir, "s01-review")
    review = stage_output("growth-review", "test-run", "s01-review", 1)
    if copy_warning:
        review["role_payload"]["data_quality_warnings"] = [
            {
                "artifact_id": record["artifact_id"],
                "sha256": record["sha256"],
                "query_id": "q-quality",
                "query_revision": 1,
                "warning": warning,
            }
        ]
        state = materialize_stage(run_dir, review)
        assert state["status"] == "awaiting_user_confirmation"
    else:
        with pytest.raises(ValueError, match="data_quality_warnings must exactly match"):
            materialize_stage(run_dir, review)


def test_report_must_inherit_pass_with_risks_finding(initialized_run) -> None:
    run_dir, _ = initialized_run(["growth-review", "growth-report"])
    result = run_dir / "data" / "result.csv"
    result.write_text("day,revenue_total\n2026-01-01,10\n", encoding="utf-8")
    register_artifacts(run_dir, [{"kind": "user_result", "path": result}])
    approve_pending(run_dir, "route", "route-risk-key")

    start_stage(run_dir, "s01-review")
    review = stage_output(
        "growth-review",
        "test-run",
        "s01-review",
        1,
        status="PASS_WITH_RISKS",
        review_risks=["Small sample."],
    )
    review["role_payload"]["findings"] = [
        {
            "finding_id": "finding-small-sample",
            "severity": "P2",
            "summary": "The sample is too small for a stable trend.",
            "evidence_refs": ["data/result.csv"],
            "responsible_stage": None,
        }
    ]
    materialize_stage(run_dir, review)
    approve_pending(run_dir, "stage", "review-risk-key")

    start_stage(run_dir, "s02-report")
    report = stage_output(
        "growth-report",
        "test-run",
        "s02-report",
        1,
        review_risks=["Small sample."],
    )
    with pytest.raises(ValueError, match="omitted Review caveats"):
        materialize_stage(run_dir, report)
