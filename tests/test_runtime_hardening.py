from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import pytest

import runtime_common
from build_lineage import build
from conftest import approve_pending, materialize_stage, metric, route_plan, stage_output, write_json
from runctl import (
    approve,
    audit_run,
    ingest_artifact,
    load_state,
    recover_state,
    register_artifacts,
    request_metric_edit,
    resume,
    revise,
    set_route,
    start_stage,
    stop,
)
from runtime_common import RunLock, append_jsonl, read_jsonl, utc_now


def run_stage(run_dir: Path, role: str, stage_id: str, **kwargs):
    attempt_dir = start_stage(run_dir, stage_id)
    output = stage_output(role, "test-run", stage_id, int(attempt_dir.name.removeprefix("attempt-")), **kwargs)
    return output, materialize_stage(run_dir, output)


def test_mutation_rejects_a_tampered_state_head(initialized_run) -> None:
    run_dir, _ = initialized_run(["growth-business"])
    state = load_state(run_dir)
    pending = state["pending_approval"]
    state["status"] = "initialized"
    (run_dir / "run-state.json").write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(ValueError, match="run recover"):
        approve(
            run_dir,
            "route",
            pending["subject_id"],
            pending["subject_revision"],
            pending["subject_sha256"],
            "confirm_route",
            "approve route",
            "tampered-state-key",
        )


@pytest.mark.parametrize("mode", ["deleted", "rewritten"])
def test_approval_log_is_rebuilt_from_committed_events(initialized_run, mode: str) -> None:
    run_dir, _ = initialized_run(["growth-business"])
    approve_pending(run_dir, "route", f"approval-recovery-{mode}")
    approvals_path = run_dir / "approvals.jsonl"
    if mode == "deleted":
        approvals_path.write_text("", encoding="utf-8")
    else:
        approval = json.loads(approvals_path.read_text(encoding="utf-8"))
        approval["user_text"] = "rewritten approval"
        approvals_path.write_text(json.dumps(approval) + "\n", encoding="utf-8")

    assert any("approval" in error.lower() for error in audit_run(run_dir))
    recover_state(run_dir)
    assert audit_run(run_dir) == []


def test_stage_tampering_blocks_approval(approved_run) -> None:
    run_dir, _ = approved_run(["growth-business", "growth-review"])
    _, state = run_stage(run_dir, "growth-business", "s01-business")
    stage_path = run_dir / state["stages"][0]["artifact"]["json_path"]
    value = json.loads(stage_path.read_text(encoding="utf-8"))
    value["summary"] = "tampered"
    stage_path.write_text(json.dumps(value) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="hash changed"):
        approve_pending(run_dir, "stage", "tampered-stage-key")


def test_route_cannot_reuse_downstream_without_its_dependency(approved_run, tmp_path: Path) -> None:
    run_dir, _ = approved_run(["growth-business", "growth-metrics", "growth-review"])
    run_stage(run_dir, "growth-business", "s01-business")
    approve_pending(run_dir, "stage", "reuse-parent-key")
    run_stage(run_dir, "growth-metrics", "s02-metrics")
    approve_pending(run_dir, "stage", "reuse-child-key")
    metrics_stage = load_state(run_dir)["stages"][1]
    route = route_plan(
        ["growth-business", "growth-metrics", "growth-review"],
        revision=2,
        statuses={"s02-metrics": "approved"},
        reused=[
            {
                "stage_id": "s02-metrics",
                "artifact_sha256": metrics_stage["artifact"]["sha256"],
                "input_hashes": metrics_stage["input_hashes"],
            }
        ],
    )
    with pytest.raises(ValueError, match="reusable approved artifact"):
        set_route(run_dir, write_json(tmp_path / "route-2.json", route))


def test_running_stage_must_be_recovered_before_revision(approved_run) -> None:
    run_dir, _ = approved_run(["growth-business", "growth-review"])
    start_stage(run_dir, "s01-business")
    with pytest.raises(ValueError, match="Cannot revise"):
        revise(run_dir, "s01-business", "change scope")
    state = resume(run_dir)
    assert state["status"] == "revising"
    assert state["stages"][0]["runtime"]["failure_class"] == "interrupted"


def test_blocked_run_can_stop_and_resume_to_a_startable_revision(approved_run) -> None:
    run_dir, _ = approved_run(["growth-business", "growth-review"])
    run_stage(run_dir, "growth-business", "s01-business", status="BLOCKED")
    stop(run_dir, "pause")
    resumed = resume(run_dir)
    assert resumed["status"] == "revising"
    assert resumed["stages"][0]["status"] == "revising"
    assert start_stage(run_dir, "s01-business").name == "attempt-2"


def test_pending_review_rollback_survives_stop_and_resume(approved_run) -> None:
    run_dir, _ = approved_run(["growth-business", "growth-review", "growth-report"])
    run_stage(run_dir, "growth-business", "s01-business")
    approve_pending(run_dir, "stage", "rollback-parent-key")
    run_stage(run_dir, "growth-review", "s02-review", status="FAIL")
    stop(run_dir, "pause rollback")
    resumed = resume(run_dir)
    assert resumed["status"] == "failed"
    assert resumed["pending_approval"]["approval_type"] == "rollback"
    with pytest.raises(ValueError, match="rollback must be approved"):
        revise(run_dir, "s01-business", "bypass rollback")


def test_review_cannot_rollback_to_an_unrelated_route_branch(initialized_run) -> None:
    run_dir, route = initialized_run(
        ["growth-business", "growth-metrics", "growth-insight", "growth-review", "growth-report"]
    )
    route["stages"][1]["depends_on"] = []
    route["stages"][2]["depends_on"] = ["s01-business"]
    route["stages"][3]["depends_on"] = ["s03-insight"]
    route["stages"][4]["depends_on"] = ["s04-review"]
    set_route(run_dir, write_json(run_dir.parent.parent / "branched-route.json", {**route, "route_revision": 2}))
    approve_pending(run_dir, "route", "branched-route-key")
    run_stage(run_dir, "growth-business", "s01-business")
    approve_pending(run_dir, "stage", "branched-business-key")
    run_stage(run_dir, "growth-metrics", "s02-metrics")
    approve_pending(run_dir, "stage", "branched-metrics-key")
    run_stage(run_dir, "growth-insight", "s03-insight")
    approve_pending(run_dir, "stage", "branched-insight-key")
    lineage = build(run_dir)
    expected_breaks = [
        f"{item['metric_id']}:{lineage_break}"
        for item in lineage["metrics"]
        for lineage_break in item["breaks"]
    ]
    attempt = start_stage(run_dir, "s04-review")
    review = stage_output("growth-review", "test-run", "s04-review", int(attempt.name.removeprefix("attempt-")), status="FAIL")
    review["role_payload"]["rollback_stage"] = "s02-metrics"
    review["role_payload"]["lineage_breaks"] = expected_breaks
    with pytest.raises(ValueError, match="must be an ancestor"):
        materialize_stage(run_dir, review)


def test_upstream_revision_invalidates_old_query_results(approved_run, tmp_path: Path) -> None:
    run_dir, _ = approved_run(["growth-metrics", "growth-sql", "growth-insight", "growth-review"])
    run_stage(run_dir, "growth-metrics", "s01-metrics")
    approve_pending(run_dir, "stage", "invalidate-metrics-key")
    run_stage(run_dir, "growth-sql", "s02-sql")
    approve_pending(run_dir, "stage", "invalidate-sql-key")
    result_path = tmp_path / "old-result.csv"
    result_path.write_text("revenue_total\n10\n", encoding="utf-8")
    registered = ingest_artifact(run_dir, result_path, "query_result")

    revise(run_dir, "s01-metrics", "change metric definition")
    stale = next(item for item in load_state(run_dir)["artifacts"] if item.get("artifact_id") == registered["artifact_id"])
    assert stale.get("superseded_by")


def test_missing_attempt_directory_is_recovered_as_interrupted(approved_run) -> None:
    run_dir, _ = approved_run(["growth-business", "growth-review"])
    attempt = start_stage(run_dir, "s01-business")
    shutil.rmtree(attempt)
    resumed = resume(run_dir)
    assert resumed["status"] == "revising"
    assert attempt.is_dir()
    assert start_stage(run_dir, "s01-business").name == "attempt-2"


def test_structured_metric_addition_must_appear_in_revision(approved_run, tmp_path: Path) -> None:
    run_dir, _ = approved_run(["growth-metrics", "growth-review"])
    run_stage(run_dir, "growth-metrics", "s01-metrics")
    state = load_state(run_dir)
    added = metric("orders_total")
    added["name"] = "Orders"
    added["formula"] = "COUNT(order_id)"
    added["field_dependencies"] = ["orders.order_id"]
    changes = {key: value for key, value in added.items() if key not in {"metric_id", "version"}}
    edit = {
        "schema_version": "1.1",
        "edit_id": "edit-add-orders",
        "run_id": state["run_id"],
        "stage_id": "s01-metrics",
        "operation": "add",
        "metric_id": "orders_total",
        "requested_changes": changes,
        "user_text": "Add total orders.",
        "base_artifact_sha256": state["stages"][0]["artifact"]["sha256"],
        "created_at": utc_now(),
    }
    request_metric_edit(run_dir, write_json(tmp_path / "metric-edit.json", edit))
    attempt = start_stage(run_dir, "s01-metrics")
    revised = stage_output(
        "growth-metrics",
        "test-run",
        "s01-metrics",
        int(attempt.name.removeprefix("attempt-")),
        metrics=[metric(), added],
    )
    materialize_stage(run_dir, revised)
    assert load_state(run_dir)["pending_metric_edit"] is None


def test_recovery_discards_a_partial_final_event_line(initialized_run) -> None:
    run_dir, _ = initialized_run(["growth-business"])
    with (run_dir / "events.jsonl").open("ab") as handle:
        handle.write(b'{"partial_event":')
    with pytest.raises(ValueError, match="Invalid JSONL"):
        load_state(run_dir)
    recovered = recover_state(run_dir)
    assert recovered["status"] == "awaiting_route_confirmation"
    assert audit_run(run_dir) == []


def test_append_jsonl_retries_short_writes(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "events.jsonl"
    original_write = runtime_common.os.write

    def short_write(fd: int, value) -> int:
        return original_write(fd, bytes(value[:3]))

    monkeypatch.setattr(runtime_common.os, "write", short_write)
    append_jsonl(output, {"value": "complete"})
    assert read_jsonl(output) == [{"value": "complete"}]


def test_os_lock_blocks_a_second_owner_then_releases(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    outcomes: list[str] = []

    with RunLock(run_dir, timeout_seconds=1):
        def contend() -> None:
            try:
                with RunLock(run_dir, timeout_seconds=0.1):
                    outcomes.append("acquired")
            except TimeoutError:
                outcomes.append("blocked")

        thread = threading.Thread(target=contend)
        thread.start()
        thread.join()
    assert outcomes == ["blocked"]
    with RunLock(run_dir, timeout_seconds=0.2):
        assert (run_dir / ".run.lock").is_file()
