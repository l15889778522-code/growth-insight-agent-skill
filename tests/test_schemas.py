from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from conftest import ROOT, stage_output
from validate_stage_output import extract_single_json, validate_stage


def test_all_json_schemas_are_valid() -> None:
    schema_paths = list((ROOT / "schemas").rglob("*.json"))
    assert len(schema_paths) >= 15
    for path in schema_paths:
        Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))


def test_custom_agents_are_native_read_only_contracts() -> None:
    paths = sorted((ROOT / "assets" / "custom-agents").glob("*.toml"))
    assert len(paths) == 7
    names = set()
    expected_efforts = {
        "growth-business": "medium",
        "growth-metrics": "high",
        "growth-sql": "max",
        "growth-insight": "high",
        "growth-visualization": "medium",
        "growth-review": "high",
        "growth-report": "high",
    }
    for path in paths:
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
        names.add(parsed["name"])
        assert parsed["sandbox_mode"] == "read-only"
        assert parsed["model_reasoning_effort"] == expected_efforts[parsed["name"]]
        if parsed["name"] == "growth-sql":
            assert parsed["model"] == "gpt-5.6-luna"
        else:
            assert "model" not in parsed
        assert "model_provider" not in parsed
        assert "deepseek" not in path.read_text(encoding="utf-8").lower()
        assert "agent_contract_version" in parsed["developer_instructions"]
    assert names == {
        "growth-business",
        "growth-metrics",
        "growth-sql",
        "growth-insight",
        "growth-visualization",
        "growth-review",
        "growth-report",
    }


def test_response_must_be_exactly_one_json_object() -> None:
    assert extract_single_json('{"ok": true}') == {"ok": True}
    with pytest.raises(ValueError):
        extract_single_json('Here: {"ok": true}')
    with pytest.raises(ValueError, match="outside"):
        extract_single_json('{"ok": true}\nextra')


def test_review_decision_and_stage_status_must_agree() -> None:
    value = stage_output("growth-review", "test-run", "s01-review", 1)
    value["role_payload"]["decision"] = "FAIL"
    errors = validate_stage(value, "growth-review", "test-run", "s01-review", 1)
    assert any("must equal" in error for error in errors)


def test_report_must_end_route() -> None:
    value = stage_output("growth-report", "test-run", "s02-report", 1)
    value["recommended_next_stage"] = "another-stage"
    errors = validate_stage(value, "growth-report")
    assert any("terminate" in error for error in errors)
