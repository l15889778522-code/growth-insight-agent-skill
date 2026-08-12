from __future__ import annotations

import json
from pathlib import Path

import pytest

from runctl import (
    EVENT_CHECKPOINT_INTERVAL,
    _apply_state_delta,
    _commit,
    _event_state_after,
    _state_delta,
    audit_run,
    initialize_run,
    load_state,
    recover_state,
)
from runtime_common import canonical_json_bytes, read_jsonl


def test_state_delta_round_trips_nested_objects_and_lists() -> None:
    before = {
        "object": {"keep": 1, "change": None, "remove": True},
        "list": [{"status": "old"}, 2, 3],
    }
    after = {
        "object": {"keep": 1, "change": "present", "add": False},
        "list": [{"status": "new"}, 2, 3, {"added": None}],
    }

    operations = _state_delta(before, after)

    assert _apply_state_delta(before, operations) == after
    assert _apply_state_delta(after, _state_delta(after, before)) == before


def _long_history(run_dir: Path, event_count: int = 56) -> list[dict]:
    state = load_state(run_dir)
    state["artifacts"][0]["metadata"]["checkpoint_test_padding"] = "x" * 32_768
    while state["last_event_id"] < event_count:
        state["runtime"]["surface"] = f"checkpoint-test-{state['last_event_id']}"
        _commit(run_dir, state, "checkpoint_test_event", {"sequence": state["last_event_id"] + 1})
    return read_jsonl(run_dir / "events.jsonl")


def test_checkpoint_history_is_smaller_auditable_and_recoverable(tmp_path: Path) -> None:
    run_dir = initialize_run(tmp_path / "runs", "checkpoint-history", {"question": "x"})
    events = _long_history(run_dir)

    assert events[0]["state_encoding"] == "snapshot"
    assert events[1]["state_encoding"] == "delta"
    for event_id in range(EVENT_CHECKPOINT_INTERVAL, len(events) + 1, EVENT_CHECKPOINT_INTERVAL):
        assert events[event_id - 1]["state_encoding"] == "snapshot"

    previous = None
    equivalent_full_snapshot_bytes = 0
    for event in events:
        previous = _event_state_after(event, previous)
        expanded = {
            key: value
            for key, value in event.items()
            if key not in {"event_sha256", "state_delta", "state_encoding"}
        }
        expanded["state_after"] = previous
        equivalent_full_snapshot_bytes += len(canonical_json_bytes(expanded)) + 1
    assert (run_dir / "events.jsonl").stat().st_size < equivalent_full_snapshot_bytes // 3
    assert audit_run(run_dir) == []

    state_path = run_dir / "run-state.json"
    damaged = json.loads(state_path.read_text(encoding="utf-8"))
    damaged["status"] = "running"
    state_path.write_text(json.dumps(damaged), encoding="utf-8")
    recovered = recover_state(run_dir)

    assert recovered["runtime"]["surface"] == f"checkpoint-test-{len(events) - 1}"
    assert audit_run(run_dir) == []


def test_tampered_delta_breaks_content_and_state_chains(tmp_path: Path) -> None:
    run_dir = initialize_run(tmp_path / "runs", "checkpoint-tamper", {"question": "x"})
    events = _long_history(run_dir, event_count=3)
    delta = next(event for event in events if event.get("state_encoding") == "delta")
    set_operation = next(operation for operation in delta["state_delta"] if operation["op"] == "set")
    set_operation["value"] = "tampered"
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )

    errors = audit_run(run_dir)
    assert any("content hash" in error for error in errors)
    assert any("state hash" in error or "state hash chain" in error for error in errors)
    with pytest.raises(ValueError, match="cannot be recovered"):
        recover_state(run_dir)
