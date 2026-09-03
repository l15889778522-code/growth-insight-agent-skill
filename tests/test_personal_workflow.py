from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import bind_route_inputs, route_plan, stage_output, write_json
from render_stage_report import render_markdown
from runctl import (
    approve,
    auto_approve_current_stage,
    check_stage_parallel,
    compact_state,
    initialize_run,
    load_state,
    prevalidate_stage_response,
    record_agent_closed,
    record_agent_receipt,
    record_stage,
    set_route,
    start_stage,
)


def _approved_personal_run(tmp_path: Path, roles: list[str], *, policy: str = "key-gates") -> Path:
    run_dir = initialize_run(
        tmp_path / "runs",
        "personal-run",
        {"question": "Why did activity decline?"},
        workflow_mode="personal",
        approval_policy=policy,
    )
    route = bind_route_inputs(run_dir, route_plan(roles))
    set_route(run_dir, write_json(tmp_path / "route.json", route))
    pending = load_state(run_dir)["pending_approval"]
    approve(
        run_dir,
        "route",
        pending["subject_id"],
        pending["subject_revision"],
        pending["subject_sha256"],
        "confirm_route",
        "confirm personal route",
        "personal-route-approval",
    )
    return run_dir


def _prevalidate_and_record(run_dir: Path, role: str, stage_id: str) -> dict:
    attempt_dir = start_stage(run_dir, stage_id)
    state = load_state(run_dir)
    output = stage_output(role, state["run_id"], stage_id, int(attempt_dir.name.removeprefix("attempt-")))
    raw = attempt_dir / "raw-response.txt"
    raw.write_text(json.dumps(output, ensure_ascii=False) + "\n", encoding="utf-8")
    stage_json = attempt_dir / "stage.json"
    validation = attempt_dir / "validation.json"
    report = prevalidate_stage_response(run_dir, raw, stage_json, validation)
    assert report["valid"] is True
    markdown = attempt_dir / "stage.md"
    markdown.write_text(render_markdown(output) + "\n", encoding="utf-8")
    record_agent_receipt(
        run_dir,
        stage_id,
        raw,
        stage_json,
        agent_id=f"agent-{stage_id}",
        model="gpt-test",
        input_tokens=10,
        output_tokens=5,
        capture_method="codex_tool_result",
    )
    return record_stage(run_dir, stage_json, markdown, validation)


def test_new_runs_record_personal_defaults_and_compact_state(tmp_path: Path) -> None:
    run_dir = initialize_run(tmp_path / "runs", "personal-default", {"question": "x"})
    state = load_state(run_dir)
    assert state["workflow_mode"] == "personal"
    assert state["approval_policy"] == "key-gates"
    compact = compact_state(state)
    assert compact["workflow_mode"] == "personal"
    assert "artifacts" not in compact
    assert "runtime" not in compact


def test_strict_mode_rejects_automatic_approval_policy(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Strict mode"):
        initialize_run(
            tmp_path / "runs",
            "strict-invalid",
            {"question": "x"},
            workflow_mode="strict",
            approval_policy="key-gates",
        )


def test_personal_mode_auto_approves_only_after_agent_closes(tmp_path: Path) -> None:
    run_dir = _approved_personal_run(tmp_path, ["growth-business", "growth-metrics"])
    _prevalidate_and_record(run_dir, "growth-business", "s01-business")
    assert compact_state(load_state(run_dir))["auto_approval_eligible"] is True
    with pytest.raises(ValueError, match="Close the completed Agent"):
        auto_approve_current_stage(run_dir)
    record_agent_closed(run_dir, "s01-business", "closed")
    auto_approve_current_stage(run_dir)
    state = load_state(run_dir)
    assert state["stages"][0]["status"] == "approved"
    assert state["pending_stage"] == "s02-metrics"


def test_key_gate_role_still_requires_user_approval(tmp_path: Path) -> None:
    run_dir = _approved_personal_run(tmp_path, ["growth-metrics"])
    _prevalidate_and_record(run_dir, "growth-metrics", "s01-metrics")
    record_agent_closed(run_dir, "s01-metrics", "closed")
    with pytest.raises(ValueError, match="required user confirmation gate"):
        auto_approve_current_stage(run_dir)


def test_stage_parallel_check_is_bounded_and_shares_frozen_inputs(tmp_path: Path) -> None:
    run_dir = initialize_run(
        tmp_path / "runs",
        "parallel-run",
        {"question": "Why did activity decline?"},
        workflow_mode="personal",
        approval_policy="key-gates",
    )
    route = route_plan(["growth-insight"])
    route["stages"][0]["parallel"] = {
        "enabled": True,
        "max_agents": 2,
        "merge_required": True,
    }
    bound = bind_route_inputs(run_dir, route)
    set_route(run_dir, write_json(tmp_path / "parallel-route.json", bound))
    pending = load_state(run_dir)["pending_approval"]
    approve(
        run_dir,
        "route",
        pending["subject_id"],
        pending["subject_revision"],
        pending["subject_sha256"],
        "confirm_route",
        "confirm parallel route",
        "parallel-route-approval",
    )
    attempt_dir = start_stage(run_dir, "s01-insight")
    state = load_state(run_dir)
    checked = check_stage_parallel(
        run_dir,
        "s01-insight",
        2,
        ["data quality", "segment comparison"],
    )
    assert checked["allowed"] is True
    assert checked["attempt"] == 1
    assert checked["shared_input_hashes"] == state["stages"][0]["input_hashes"]
    assert attempt_dir.is_dir()

    with pytest.raises(ValueError, match="exactly two"):
        check_stage_parallel(run_dir, "s01-insight", 3, ["a", "b", "c"])
    with pytest.raises(ValueError, match="distinct"):
        check_stage_parallel(run_dir, "s01-insight", 2, ["same", "same"])


def test_failed_agent_close_can_be_retried_before_auto_approval(tmp_path: Path) -> None:
    run_dir = _approved_personal_run(tmp_path, ["growth-business"])
    _prevalidate_and_record(run_dir, "growth-business", "s01-business")
    record_agent_closed(run_dir, "s01-business", "failed")
    assert compact_state(load_state(run_dir))["agent_close_pending"] is True
    with pytest.raises(ValueError, match="Close the completed Agent"):
        auto_approve_current_stage(run_dir)
    record_agent_closed(run_dir, "s01-business", "closed")
    auto_approve_current_stage(run_dir)
    assert load_state(run_dir)["stages"][0]["status"] == "approved"


def test_prevalidation_detects_changed_bound_input_before_receipt(tmp_path: Path) -> None:
    run_dir = _approved_personal_run(tmp_path, ["growth-business"])
    attempt_dir = start_stage(run_dir, "s01-business")
    output = stage_output("growth-business", "personal-run", "s01-business", 1)
    raw = attempt_dir / "raw-response.txt"
    raw.write_text(json.dumps(output, ensure_ascii=False) + "\n", encoding="utf-8")
    (run_dir / "request.json").write_text('{"question":"changed"}\n', encoding="utf-8")
    report = prevalidate_stage_response(
        run_dir,
        raw,
        attempt_dir / "stage.json",
        attempt_dir / "validation.json",
    )
    assert report["valid"] is False
    assert any("changed while the stage was running" in error for error in report["errors"])
    assert not (attempt_dir / "stage.json").exists()
