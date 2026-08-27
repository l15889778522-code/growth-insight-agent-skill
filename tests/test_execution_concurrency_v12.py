from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
from types import ModuleType
from typing import Any

import pytest

import build_lineage as lineage_builder
import render_charts as chart_renderer
import runctl
from conftest import approve_pending, materialize_stage, stage_output, write_json
from runctl import (
    audit_run,
    begin_execution_lease,
    finalize_run,
    ingest_artifact,
    load_state,
    prepare_query,
    publish_final_report,
    record_query_result,
    revise,
    start_stage,
    stop,
)
from runtime_common import load_json, sha256_file, utc_now


SYNC_TIMEOUT_SECONDS = 10
QUERY_SQL = "SELECT day, SUM(revenue) AS revenue_total FROM sales GROUP BY day"


def _run_stage(run_dir: Path, role: str, stage_id: str, **output_kwargs: Any) -> dict[str, Any]:
    attempt_dir = start_stage(run_dir, stage_id)
    output = stage_output(
        role,
        load_state(run_dir)["run_id"],
        stage_id,
        int(attempt_dir.name.removeprefix("attempt-")),
        **output_kwargs,
    )
    materialize_stage(run_dir, output)
    return output


def _run_and_approve_stage(
    run_dir: Path,
    role: str,
    stage_id: str,
    approval_key: str,
    **output_kwargs: Any,
) -> dict[str, Any]:
    output = _run_stage(run_dir, role, stage_id, **output_kwargs)
    approve_pending(run_dir, "stage", approval_key)
    return output


def _approved_sql_run(approved_run) -> Path:
    run_dir, _ = approved_run(["growth-metrics", "growth-sql", "growth-review"])
    _run_and_approve_stage(run_dir, "growth-metrics", "s01-metrics", "concurrency-metrics")
    _run_and_approve_stage(
        run_dir,
        "growth-sql",
        "s02-sql",
        "concurrency-sql",
        sql=QUERY_SQL,
    )
    return run_dir


def _prepare_approved_query(
    run_dir: Path,
    tmp_path: Path,
    *,
    expected_revision: int,
) -> dict[str, Any]:
    sql_path = tmp_path / f"query-r{expected_revision}.sql"
    sql_path.write_text(QUERY_SQL, encoding="utf-8")
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
    assert request["revision"] == expected_revision
    approve_pending(run_dir, "query", f"concurrency-query-r{expected_revision}")
    return request


def _begin_query_lease(run_dir: Path, request: dict[str, Any]) -> dict[str, Any]:
    input_ids = [
        item["artifact_id"]
        for item in load_state(run_dir)["artifacts"]
        if item.get("kind") in {"query_request", "query_sql"}
        and item.get("metadata", {}).get("query_id") == request["query_id"]
        and item.get("metadata", {}).get("query_revision") == request["revision"]
    ]
    assert {item.split("-", 1)[0] for item in input_ids} == {"query_request", "query_sql"}
    return begin_execution_lease(
        run_dir,
        "query",
        request["query_id"],
        request["revision"],
        input_artifact_ids=input_ids,
    )


def _stage_query_result(
    run_dir: Path,
    request: dict[str, Any],
    lease: dict[str, Any],
    value: int,
) -> tuple[Path, Path, Path]:
    staging = run_dir / lease["staging_path"]
    result_path = staging / "result.csv"
    result_path.write_text(f"revenue_total\n{value}\n", encoding="utf-8")
    profile_path = write_json(
        staging / "result-profile.json",
        {
            "schema_version": "1.2",
            "row_count": 1,
            "profiling": {
                "memory_model": "bounded_per_column",
                "distinct_exact_max_values": 100,
                "distinct_exact_max_utf8_bytes": 4096,
                "distinct_approximate_max_hashes": 64,
                "sample_max_values": 20,
                "sample_max_value_utf8_bytes": 1024,
                "sample_max_total_utf8_bytes": 4096,
            },
            "columns": [],
        },
    )
    output_path = lease["output_path"]
    manifest_path = write_json(
        staging / "query-manifest.json",
        {
            "schema_version": "1.2",
            "query_id": request["query_id"],
            "query_revision": request["revision"],
            "metric_ids": request["metric_ids"],
            "data_source_type": "sqlite",
            "data_source_id": request["data_source_id"],
            "data_source_fingerprint": request["data_source_fingerprint"],
            "sql_sha256": request["sql_sha256"],
            "source_sql_sha256": request["source_sql_sha256"],
            "sql_canonicalization": request["sql_canonicalization"],
            "dialect": request["dialect"],
            "started_at": utc_now(),
            "completed_at": utc_now(),
            "elapsed_ms": 0,
            "timeout_seconds": request["timeout_seconds"],
            "max_rows": request["max_rows"],
            "max_result_bytes": request["max_result_bytes"],
            "returned_rows": 1,
            "columns": [],
            "result_path": f"{output_path}/result.csv",
            "result_bytes": result_path.stat().st_size,
            "result_sha256": sha256_file(result_path),
            "profile_path": f"{output_path}/result-profile.json",
            "profile_sha256": sha256_file(profile_path),
            "truncated": False,
            "sampled": False,
            "quality_warnings": [],
        },
    )
    return manifest_path, result_path, profile_path


@pytest.mark.parametrize("boundary", ["stop", "revise"])
def test_query_boundary_invalidates_in_flight_publication(
    approved_run,
    tmp_path: Path,
    boundary: str,
) -> None:
    run_dir = _approved_sql_run(approved_run)
    request = _prepare_approved_query(run_dir, tmp_path, expected_revision=1)
    lease = _begin_query_lease(run_dir, request)
    paths = _stage_query_result(run_dir, request, lease, 11)
    worker_ready = Event()
    release_worker = Event()

    def publish_after_boundary() -> dict[str, Any]:
        worker_ready.set()
        if not release_worker.wait(SYNC_TIMEOUT_SECONDS):
            raise TimeoutError("query publication boundary was not released")
        return record_query_result(run_dir, *paths, lease_id=lease["lease_id"])

    with ThreadPoolExecutor(max_workers=1) as pool:
        publication = pool.submit(publish_after_boundary)
        try:
            assert worker_ready.wait(SYNC_TIMEOUT_SECONDS)
            if boundary == "stop":
                boundary_state = stop(run_dir, "cancel in-flight query")
                assert boundary_state["status"] == "stopped"
            else:
                boundary_state = revise(run_dir, "s02-sql", "revise the approved query")
                assert boundary_state["status"] == "revising"
            saved = next(
                item for item in boundary_state["execution_leases"] if item["lease_id"] == lease["lease_id"]
            )
            assert saved["state"] == "aborted"
        finally:
            release_worker.set()

        with pytest.raises(ValueError, match="not publishable"):
            publication.result(timeout=SYNC_TIMEOUT_SECONDS)

    state = load_state(run_dir)
    saved = next(item for item in state["execution_leases"] if item["lease_id"] == lease["lease_id"])
    assert saved["state"] == "aborted"
    assert not any(
        item.get("kind") == "query_result"
        and item.get("metadata", {}).get("lease_id") == lease["lease_id"]
        for item in state["artifacts"]
    )
    assert not (run_dir / lease["output_path"]).exists()


def test_repeated_query_revisions_preserve_immutable_history(approved_run, tmp_path: Path) -> None:
    run_dir = _approved_sql_run(approved_run)
    published: list[tuple[Path, str, bytes]] = []

    for revision in range(1, 4):
        request = _prepare_approved_query(run_dir, tmp_path, expected_revision=revision)
        lease = _begin_query_lease(run_dir, request)
        paths = _stage_query_result(run_dir, request, lease, revision * 10)
        record_query_result(run_dir, *paths, lease_id=lease["lease_id"])
        result_path = run_dir / lease["output_path"] / "result.csv"
        published.append((result_path, sha256_file(result_path), result_path.read_bytes()))

    state = load_state(run_dir)
    request_revisions = sorted(
        item["metadata"]["query_revision"]
        for item in state["artifacts"]
        if item.get("kind") == "query_request"
        and item.get("metadata", {}).get("query_id") == "q-revenue"
    )
    result_revisions = sorted(
        item["metadata"]["query_revision"]
        for item in state["artifacts"]
        if item.get("kind") == "query_result"
        and item.get("metadata", {}).get("query_id") == "q-revenue"
    )

    assert request_revisions == [1, 2, 3]
    assert result_revisions == [1, 2, 3]
    assert [path.parent.parent.name for path, _, _ in published] == [
        "revision-1",
        "revision-2",
        "revision-3",
    ]
    assert all(path.is_file() and sha256_file(path) == digest and path.read_bytes() == content for path, digest, content in published)
    assert [lease["state"] for lease in state["execution_leases"] if lease["kind"] == "query"] == [
        "completed",
        "completed",
        "completed",
    ]
    assert audit_run(run_dir) == []


def _chart_spec(source: dict[str, Any], title: str) -> dict[str, Any]:
    return {
        "schema_version": "1.2",
        "source_file": source["path"],
        "source_sha256": source["sha256"],
        "charts": [
            {
                "chart_id": "revenue-trend",
                "title": title,
                "type": "line",
                "question": "How is revenue changing?",
                "metric_id": "revenue_total",
                "x": "day",
                "y": "revenue_total",
                "filters": [],
                "aggregation": "none",
                "output_formats": ["html"],
                "status": "blocked_by_missing_data",
            }
        ],
    }


def _gate_first_publication_return(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
) -> tuple[Event, Event]:
    first_publication_returned = Event()
    release_first_publication = Event()
    counter_lock = Lock()
    publication_calls = 0
    original = module.publish_execution_artifacts

    def gated_publication(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        nonlocal publication_calls
        records = original(*args, **kwargs)
        with counter_lock:
            publication_calls += 1
            call_number = publication_calls
        if call_number == 1:
            first_publication_returned.set()
            if not release_first_publication.wait(SYNC_TIMEOUT_SECONDS):
                raise TimeoutError("first execution publication was not released")
        return records

    monkeypatch.setattr(module, "publish_execution_artifacts", gated_publication)
    return first_publication_returned, release_first_publication


def _active_artifacts(run_dir: Path, kind: str) -> list[dict[str, Any]]:
    return [
        item
        for item in load_state(run_dir)["artifacts"]
        if item.get("kind") == kind and not item.get("superseded_by")
    ]


def test_concurrent_chart_builds_keep_latest_pointer_on_active_publication(
    approved_run,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _ = approved_run(["growth-review"])
    source_path = tmp_path / "chart-source.csv"
    source_path.write_text("day,revenue_total\n2026-01-01,10\n", encoding="utf-8")
    source = ingest_artifact(run_dir, source_path, "user_result")
    spec_path = write_json(run_dir / "charts" / "chart-specs.json", _chart_spec(source, "Stale title"))
    first_returned, release_first = _gate_first_publication_return(
        monkeypatch,
        chart_renderer,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        stale_build = pool.submit(chart_renderer.render, run_dir, spec_path)
        assert first_returned.wait(SYNC_TIMEOUT_SECONDS)
        write_json(spec_path, _chart_spec(source, "Latest title"))
        latest_build = pool.submit(chart_renderer.render, run_dir, spec_path)
        try:
            latest_manifest = latest_build.result(timeout=SYNC_TIMEOUT_SECONDS)
        finally:
            release_first.set()
        stale_manifest = stale_build.result(timeout=SYNC_TIMEOUT_SECONDS)

    active = _active_artifacts(run_dir, "chart_manifest")
    assert len(active) == 1
    pointer = load_json(run_dir / "charts" / "latest.json")
    assert stale_manifest["render_identity"] != latest_manifest["render_identity"]
    assert pointer["target_path"] == active[0]["path"]
    assert pointer["target_sha256"] == active[0]["sha256"]
    assert pointer["content_identity"] == latest_manifest["render_identity"]


def test_concurrent_lineage_builds_keep_latest_pointer_on_active_publication(
    approved_run,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _ = approved_run(["growth-metrics", "growth-sql"])
    _run_and_approve_stage(run_dir, "growth-metrics", "s01-metrics", "lineage-race-metrics")
    first_returned, release_first = _gate_first_publication_return(
        monkeypatch,
        lineage_builder,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        stale_build = pool.submit(lineage_builder.build, run_dir)
        assert first_returned.wait(SYNC_TIMEOUT_SECONDS)
        _run_and_approve_stage(
            run_dir,
            "growth-sql",
            "s02-sql",
            "lineage-race-sql",
            sql=QUERY_SQL,
        )
        latest_build = pool.submit(lineage_builder.build, run_dir)
        try:
            latest_lineage = latest_build.result(timeout=SYNC_TIMEOUT_SECONDS)
        finally:
            release_first.set()
        stale_lineage = stale_build.result(timeout=SYNC_TIMEOUT_SECONDS)

    active = _active_artifacts(run_dir, "metric_lineage_latest")
    assert len(active) == 1
    pointer = load_json(run_dir / "lineage" / "latest.json")
    assert stale_lineage != latest_lineage
    assert pointer["target_path"] == active[0]["path"]
    assert pointer["target_sha256"] == active[0]["sha256"]
    assert pointer["content_identity"] == active[0]["metadata"]["content_identity"]


def _finalizing_run(approved_run) -> Path:
    run_dir, _ = approved_run(["growth-review", "growth-report"])
    _run_and_approve_stage(run_dir, "growth-review", "s01-review", "final-boundary-review")
    _run_and_approve_stage(
        run_dir,
        "growth-report",
        "s02-report",
        "final-boundary-report",
        evidence_ref="s01-review:decision",
    )
    assert load_state(run_dir)["status"] == "finalizing"
    return run_dir


def test_stop_boundary_prevents_in_flight_final_report_from_completing(
    approved_run,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _finalizing_run(approved_run)
    report_copied = Event()
    release_publication = Event()
    original_copy = runctl.atomic_copy_file

    def gated_copy(source: Path, destination: Path) -> None:
        original_copy(source, destination)
        if destination.name == "final-report.md":
            report_copied.set()
            if not release_publication.wait(SYNC_TIMEOUT_SECONDS):
                raise TimeoutError("final report publication boundary was not released")

    monkeypatch.setattr(runctl, "atomic_copy_file", gated_copy)
    with ThreadPoolExecutor(max_workers=1) as pool:
        publication = pool.submit(publish_final_report, run_dir)
        try:
            assert report_copied.wait(SYNC_TIMEOUT_SECONDS)
            stopped = stop(run_dir, "stop before final report publication")
            assert stopped["status"] == "stopped"
        finally:
            release_publication.set()
        with pytest.raises(ValueError, match="not publishable"):
            publication.result(timeout=SYNC_TIMEOUT_SECONDS)

    state = load_state(run_dir)
    final_leases = [lease for lease in state["execution_leases"] if lease["kind"] == "final_report"]
    assert len(final_leases) == 1
    assert final_leases[0]["state"] == "aborted"
    assert not any(item.get("kind") in {"final_report", "final_report_manifest"} for item in state["artifacts"])
    assert not (run_dir / "final" / "reports").exists()


def test_finalize_rechecks_final_report_after_concurrent_tamper(
    approved_run,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _finalizing_run(approved_run)
    final_report = publish_final_report(run_dir)
    summary_started = Event()
    release_finalize = Event()
    original_build_summary = runctl.build_run_summary

    def gated_build_summary(*args: Any, **kwargs: Any) -> dict[str, Any]:
        summary_started.set()
        if not release_finalize.wait(SYNC_TIMEOUT_SECONDS):
            raise TimeoutError("finalization boundary was not released")
        return original_build_summary(*args, **kwargs)

    monkeypatch.setattr(runctl, "build_run_summary", gated_build_summary)
    with ThreadPoolExecutor(max_workers=1) as pool:
        finalization = pool.submit(finalize_run, run_dir)
        try:
            assert summary_started.wait(SYNC_TIMEOUT_SECONDS)
            (run_dir / final_report["path"]).write_text("tampered during finalization\n", encoding="utf-8")
        finally:
            release_finalize.set()
        with pytest.raises(ValueError, match="changed|hash|consistency"):
            finalization.result(timeout=SYNC_TIMEOUT_SECONDS)

    assert load_state(run_dir)["status"] != "completed"
