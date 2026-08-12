from __future__ import annotations

import json
from pathlib import Path

import pytest

import runctl
from build_lineage import build
from conftest import approve_pending, bind_route_inputs, materialize_stage, metric, route_plan, stage_output, write_json
from runctl import (
    approve,
    audit_run,
    build_run_summary,
    finalize_run,
    load_state,
    publish_final_report,
    record_agent_runtime,
    register_artifacts,
    recover_state,
    resume,
    revise,
    set_route,
    start_stage,
    stop,
)
from runtime_common import read_jsonl


def _run_stage(run_dir: Path, role: str, stage_id: str, **output_kwargs):
    attempt_dir = start_stage(run_dir, stage_id)
    state = load_state(run_dir)
    output = stage_output(role, state["run_id"], stage_id, int(attempt_dir.name.removeprefix("attempt-")), **output_kwargs)
    return output, materialize_stage(run_dir, output)


def test_route_with_missing_input_cannot_be_approved(initialized_run) -> None:
    run_dir, _ = initialized_run(
        ["growth-business", "growth-metrics"],
        required_inputs=["question", "schema"],
        provided_inputs=["question"],
    )
    state = load_state(run_dir)
    pending = state["pending_approval"]
    with pytest.raises(ValueError, match="not executable"):
        approve(
            run_dir,
            "route",
            pending["subject_id"],
            pending["subject_revision"],
            pending["subject_sha256"],
            "confirm_route",
            "approve route",
            "missing-route-key",
        )
    assert not (run_dir / "approvals.jsonl").exists()
    assert load_state(run_dir)["status"] == "awaiting_route_confirmation"


def test_stage_gates_metric_revision_and_terminal_review(approved_run) -> None:
    run_dir, _ = approved_run(["growth-business", "growth-metrics", "growth-review", "growth-report"])

    _, state = _run_stage(run_dir, "growth-business", "s01-business")
    assert state["status"] == "awaiting_user_confirmation"
    with pytest.raises(ValueError, match="Cannot start"):
        start_stage(run_dir, "s02-metrics")
    approve_pending(run_dir, "stage", "business-key-0001")

    first_metrics, state = _run_stage(run_dir, "growth-metrics", "s02-metrics")
    assert state["pending_approval"]["subject_id"] == "s02-metrics"
    revise(run_dir, "s02-metrics", "Change the formula and add a custom dimension.")
    assert load_state(run_dir)["status"] == "revising"

    start_stage(run_dir, "s02-metrics")
    second_metrics = stage_output(
        "growth-metrics",
        "test-run",
        "s02-metrics",
        2,
        metrics=[metric(version=2, formula="SUM(net_revenue)")],
    )
    materialize_stage(run_dir, second_metrics)
    assert first_metrics["role_payload"]["metrics"][0]["version"] == 1
    approve_pending(run_dir, "stage", "metrics-key-0002")

    build(run_dir)
    _, state = _run_stage(run_dir, "growth-review", "s03-review")
    assert state["status"] == "awaiting_user_confirmation"
    approve_pending(run_dir, "stage", "review-key-0001")
    final = load_state(run_dir)
    assert final["status"] == "running"
    assert final["pending_stage"] == "s04-report"
    result = run_dir / "data" / "result.csv"
    result.write_text("day,revenue_total\n2026-01-01,10\n", encoding="utf-8")
    register_artifacts(run_dir, [{"kind": "user_result", "path": result}])
    _run_stage(run_dir, "growth-report", "s04-report")
    approve_pending(run_dir, "stage", "report-key-0001")
    build(run_dir)
    publish_final_report(run_dir)
    finalize_run(run_dir)
    assert load_state(run_dir)["status"] == "completed"
    assert audit_run(run_dir) == []


def test_metric_change_without_version_increment_fails(approved_run) -> None:
    run_dir, _ = approved_run(["growth-metrics", "growth-review"])
    _, _ = _run_stage(run_dir, "growth-metrics", "s01-metrics")
    revise(run_dir, "s01-metrics", "Change formula")
    start_stage(run_dir, "s01-metrics")
    invalid = stage_output(
        "growth-metrics",
        "test-run",
        "s01-metrics",
        2,
        metrics=[metric(version=1, formula="SUM(net_revenue)")],
    )
    with pytest.raises(ValueError, match="version 2"):
        materialize_stage(run_dir, invalid)
    state = load_state(run_dir)
    assert state["status"] == "failed"
    assert state["stages"][0]["runtime"]["failure_class"] == "stage_validation_failed"


def test_blocked_stage_never_creates_approval_gate(approved_run) -> None:
    run_dir, _ = approved_run(["growth-business", "growth-review"])
    _, state = _run_stage(run_dir, "growth-business", "s01-business", status="BLOCKED")
    assert state["status"] == "blocked"
    assert state["pending_approval"] is None
    assert state["stages"][0]["status"] == "blocked"


def test_idempotency_key_is_exactly_bound(initialized_run) -> None:
    run_dir, _ = initialized_run(["growth-business"])
    state = load_state(run_dir)
    pending = state["pending_approval"]
    args = (
        run_dir,
        "route",
        pending["subject_id"],
        pending["subject_revision"],
        pending["subject_sha256"],
        "confirm_route",
        "approve route",
        "same-route-key",
    )
    first = approve(*args)
    assert approve(*args) == first
    changed = list(args)
    changed[6] = "different text"
    with pytest.raises(ValueError, match="different approval"):
        approve(*changed)
    assert len((run_dir / "approvals.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_retry_finishes_approval_left_between_append_and_commit(initialized_run, monkeypatch) -> None:
    run_dir, _ = initialized_run(["growth-business"])
    pending = load_state(run_dir)["pending_approval"]
    args = (
        run_dir,
        "route",
        pending["subject_id"],
        pending["subject_revision"],
        pending["subject_sha256"],
        "confirm_route",
        "approve route",
        "crash-route-key",
    )
    original_commit = runctl._commit

    def fail_commit(*_args, **_kwargs):
        raise OSError("simulated crash")

    monkeypatch.setattr(runctl, "_commit", fail_commit)
    with pytest.raises(OSError, match="simulated"):
        approve(*args)
    assert load_state(run_dir)["status"] == "awaiting_route_confirmation"
    assert len((run_dir / "approvals.jsonl").read_text(encoding="utf-8").splitlines()) == 1

    monkeypatch.setattr(runctl, "_commit", original_commit)
    recovered_approval = approve(*args)
    assert recovered_approval["idempotency_key"] == "crash-route-key"
    assert load_state(run_dir)["status"] == "running"
    assert audit_run(run_dir) == []


def test_route_revision_carries_only_exact_approved_artifact(approved_run, tmp_path: Path) -> None:
    run_dir, _ = approved_run(["growth-business", "growth-metrics"])
    _, _ = _run_stage(run_dir, "growth-business", "s01-business")
    approve_pending(run_dir, "stage", "business-reuse-key")
    prior = load_state(run_dir)
    business = prior["stages"][0]
    second = route_plan(
        ["growth-business", "growth-metrics"],
        revision=2,
        statuses={"s01-business": "approved"},
        reused=[
            {
                "stage_id": "s01-business",
                "artifact_sha256": business["artifact"]["sha256"],
                "input_hashes": business["input_hashes"],
            }
        ],
    )
    set_route(run_dir, write_json(tmp_path / "route-2.json", bind_route_inputs(run_dir, second)))
    rerouted = load_state(run_dir)
    assert rerouted["stages"][0]["status"] == "approved"
    assert len(rerouted["approved_artifacts"]) == 1
    approve_pending(run_dir, "route", "route-key-0002")
    assert load_state(run_dir)["pending_stage"] == "s02-metrics"
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[-1]["event_type"] == "approval_carried_forward"


def test_event_snapshot_recovers_tampered_state(initialized_run) -> None:
    run_dir, _ = initialized_run(["growth-business"])
    state_path = run_dir / "run-state.json"
    damaged = json.loads(state_path.read_text(encoding="utf-8"))
    damaged["status"] = "running"
    state_path.write_text(json.dumps(damaged), encoding="utf-8")
    assert any("last committed" in error for error in audit_run(run_dir))
    recovered = recover_state(run_dir)
    assert recovered["status"] == "awaiting_route_confirmation"
    assert audit_run(run_dir) == []
    events = read_jsonl(run_dir / "events.jsonl")
    assert events[-1]["event_type"] == "state_recovered"


def test_event_snapshot_recovers_commit_interrupted_before_state_write(tmp_path: Path, monkeypatch) -> None:
    from runctl import initialize_run

    run_dir = initialize_run(tmp_path / "runs", "test-run", {"question": "x"})
    route_path = write_json(tmp_path / "route.json", bind_route_inputs(run_dir, route_plan(["growth-business"])))
    original_write = runctl.atomic_write_json

    def fail_state_write(path, value):
        if Path(path).name == "run-state.json":
            raise OSError("simulated state write interruption")
        return original_write(path, value)

    monkeypatch.setattr(runctl, "atomic_write_json", fail_state_write)
    with pytest.raises(OSError, match="interruption"):
        set_route(run_dir, route_path)
    monkeypatch.setattr(runctl, "atomic_write_json", original_write)

    with pytest.raises(ValueError, match="run recover"):
        load_state(run_dir)
    recovered = recover_state(run_dir)
    assert recovered["status"] == "awaiting_route_confirmation"
    assert recovered["route_revision"] == 1
    assert audit_run(run_dir) == []


def test_resume_converts_interrupted_agent_to_new_attempt(approved_run) -> None:
    run_dir, _ = approved_run(["growth-business", "growth-review"])
    start_stage(run_dir, "s01-business")
    stop(run_dir, "test interruption")
    state = resume(run_dir)
    assert state["status"] == "revising"
    assert state["current_stage"] is None
    assert state["pending_stage"] == "s01-business"
    assert state["stages"][0]["status"] == "revising"
    attempt_dir = start_stage(run_dir, "s01-business")
    assert attempt_dir.name == "attempt-2"


def test_runtime_summary_records_model_tokens_and_artifact(approved_run) -> None:
    run_dir, _ = approved_run(["growth-business"])
    start_stage(run_dir, "s01-business")
    record_agent_runtime(run_dir, "s01-business", "thread-123", "gpt-test", 120, 30)
    materialize_stage(run_dir, stage_output("growth-business", "test-run", "s01-business", 1))
    approve_pending(run_dir, "stage", "summary-stage-key")
    summary = build_run_summary(run_dir, run_dir / "final" / "run-summary.json")
    assert summary["token_usage"]["total_tokens"] == 150
    assert summary["attempts"][0]["thread_id"] == "thread-123"
    assert summary["attempts"][0]["model"] == "gpt-test"
    assert (run_dir / "final" / "run-summary.json").is_file()
    assert audit_run(run_dir) == []


def test_failed_review_creates_hash_bound_rollback_gate(approved_run) -> None:
    run_dir, _ = approved_run(["growth-business", "growth-review", "growth-report"])
    _run_stage(run_dir, "growth-business", "s01-business")
    approve_pending(run_dir, "stage", "rollback-business-key")
    _, state = _run_stage(run_dir, "growth-review", "s02-review", status="FAIL")
    assert state["status"] == "failed"
    assert state["pending_approval"]["approval_type"] == "rollback"
    assert state["pending_approval"]["subject_id"] == "s01-business"
    assert (run_dir / "rollback-plan.json").is_file()

    approve_pending(run_dir, "rollback", "rollback-approval-key")
    revised = load_state(run_dir)
    assert revised["status"] == "revising"
    assert revised["pending_stage"] == "s01-business"
    assert revised["stages"][0]["status"] == "revising"
    assert revised["stages"][1]["status"] == "stale"
    assert revised["stages"][2]["status"] == "pending"
    assert revised["approved_artifacts"] == []
    assert audit_run(run_dir) == []


def test_report_requires_post_report_lineage_before_finalization(approved_run) -> None:
    run_dir, _ = approved_run(["growth-metrics", "growth-review", "growth-report"])
    _run_stage(run_dir, "growth-metrics", "s01-metrics")
    approve_pending(run_dir, "stage", "finalize-metrics-key")
    build(run_dir)
    _run_stage(run_dir, "growth-review", "s02-review")
    approve_pending(run_dir, "stage", "finalize-review-key")
    _run_stage(run_dir, "growth-report", "s03-report", evidence_ref="s02-review:decision")
    assert load_state(run_dir)["status"] == "awaiting_user_confirmation"
    approve_pending(run_dir, "stage", "finalize-report-key")
    assert load_state(run_dir)["status"] == "finalizing"
    with pytest.raises(ValueError, match="rebuilt after Report"):
        finalize_run(run_dir)
    build(run_dir)
    with pytest.raises(ValueError, match="final report"):
        finalize_run(run_dir)
    publish_final_report(run_dir)
    finalize_run(run_dir)
    assert load_state(run_dir)["status"] == "completed"
    with pytest.raises(ValueError, match="immutable"):
        build(run_dir)
    final_report = next(item for item in load_state(run_dir)["artifacts"] if item.get("kind") == "final_report")
    (run_dir / final_report["path"]).write_text("tampered\n", encoding="utf-8")
    assert any("Artifact hash mismatch" in error for error in audit_run(run_dir))
