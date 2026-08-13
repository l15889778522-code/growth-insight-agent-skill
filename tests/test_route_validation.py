from __future__ import annotations

from conftest import route_plan
from validate_route_plan import validate_route


def test_valid_complete_route() -> None:
    route = route_plan(
        [
            "growth-business",
            "growth-metrics",
            "growth-sql",
            "growth-insight",
            "growth-visualization",
            "growth-review",
            "growth-report",
        ]
    )
    errors, warnings = validate_route(route, require_executable=True)
    assert errors == []
    assert warnings == []


def test_missing_inputs_are_warning_then_execution_error() -> None:
    route = route_plan(["growth-business", "growth-metrics"], required_inputs=["question", "schema"], provided_inputs=["question"])
    errors, warnings = validate_route(route)
    assert errors == []
    assert warnings
    errors, _ = validate_route(route, require_executable=True)
    assert any("not executable" in error for error in errors)


def test_report_rejects_skipped_review() -> None:
    route = route_plan(["growth-business", "growth-review", "growth-report"], statuses={"s02-review": "skipped"})
    errors, _ = validate_route(route)
    assert any("skipped" in error for error in errors)
    assert any("requires a growth-review" in error for error in errors)


def test_missing_inputs_must_match_set_difference() -> None:
    route = route_plan(["growth-review"])
    route["missing_inputs"] = ["invented"]
    errors, _ = validate_route(route)
    assert any("exactly equal" in error for error in errors)


def test_approved_route_stage_requires_reuse_entry() -> None:
    route = route_plan(["growth-review"], statuses={"s01-review": "approved"})
    errors, _ = validate_route(route)
    assert any("reused_approved_artifacts" in error for error in errors)


def test_stage_required_input_must_be_provided_or_produced_by_an_ancestor() -> None:
    route = route_plan(["growth-business", "growth-metrics"])
    route["stages"][0]["expected_outputs"] = ["business_definition"]
    route["stages"][1]["required_inputs"] = ["business_definition"]
    route["stages"][1]["depends_on"] = []
    errors, _ = validate_route(route, require_executable=True)
    assert any("neither provided nor produced by a declared ancestor" in error for error in errors)

    route["stages"][1]["depends_on"] = [route["stages"][0]["stage_id"]]
    errors, _ = validate_route(route, require_executable=True)
    assert errors == []
