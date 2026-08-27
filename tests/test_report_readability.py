from __future__ import annotations

from copy import deepcopy

import pytest

from conftest import stage_output
from render_stage_report import ROLE_LABELS, render_markdown


@pytest.mark.parametrize("role", ROLE_LABELS)
def test_each_role_renders_prose_without_changing_its_handoff(role: str) -> None:
    stage = stage_output(role, "readability-run", "s01-stage", 1)
    stage["summary"] = "本阶段已经完成分析，仍需用户确认。"
    original = deepcopy(stage)

    markdown = render_markdown(stage)

    assert stage == original
    assert "role_payload" in stage
    assert "role_payload" not in markdown
    assert "`" not in markdown
    assert "```" not in markdown
    assert "机器可审计字段" not in markdown
    assert "Handoff" not in markdown
    assert "stage_status" not in markdown
    assert "agent_contract_version" not in markdown
    assert "## 需要你确认的内容" in markdown
    assert markdown.index("## 一句话结论") < markdown.index("## 技术详情")
    assert "readability-run" not in markdown.split("## 技术详情")[0]


def test_metrics_report_keeps_business_definition_and_formula() -> None:
    stage = stage_output("growth-metrics", "readability-run", "s02-metrics", 1)
    metric = stage["role_payload"]["metrics"][0]
    metric.update(
        name="7日活跃用户数",
        business_definition="用于判断整体活跃用户规模是否下降。",
        formula="最近7天至少发生一次有效行为的去重用户数",
        grain="按天统计",
        time_window="最近7天",
        type="core",
        status="candidate",
    )
    stage["recommended_next_stage"] = "growth-review"

    markdown = render_markdown(stage)

    for value in (metric["name"], metric["business_definition"], metric["formula"], metric["grain"], metric["time_window"]):
        assert value in markdown
    assert "指标类型：核心指标" in markdown
    assert "当前状态：候选指标" in markdown
    assert "该角色建议的下一步：质量评审" in markdown
    assert "业务背景" not in markdown
    assert "数据已经说明的事实" not in markdown


def test_sql_report_explains_query_scope_without_printing_sql() -> None:
    stage = stage_output("growth-sql", "readability-run", "s03-sql", 1)
    stage["role_payload"]["queries"][0]["purpose"] = "比较本周和上周的活跃用户数量"
    stage["role_payload"]["field_mappings"][0].update(
        calculation_grain="每位用户每天计一次",
        denominator_scope="仅包含已覆盖完整观察窗口的用户",
        exclusions=["排除测试账号"],
    )

    markdown = render_markdown(stage)

    assert "比较本周和上周的活跃用户数量" in markdown
    assert "每位用户每天计一次" in markdown
    assert "仅包含已覆盖完整观察窗口的用户" in markdown
    assert "排除测试账号" in markdown
    assert stage["role_payload"]["queries"][0]["sql"] not in markdown
    assert "独立批准" in markdown


@pytest.mark.parametrize("status", ["BLOCKED", "FAIL"])
def test_unsuccessful_report_does_not_offer_approval(status: str) -> None:
    stage = stage_output("growth-insight", "readability-run", "s04-insight", 1, status=status)
    markdown = render_markdown(stage)

    assert "本阶段暂不能批准" in markdown
    assert "你可以确认本阶段" not in markdown
    if status == "BLOCKED":
        assert stage["required_next_inputs"][0] in markdown


def test_long_risk_and_fact_lists_are_not_silently_dropped() -> None:
    stage = stage_output("growth-business", "readability-run", "s01-business", 1)
    stage["facts"] = [f"事实 {index}" for index in range(8)]
    stage["risks"] = [f"风险 {index}" for index in range(8)]
    stage["conflicts"] = ["用户范围仍存在冲突"]
    markdown = render_markdown(stage)

    for item in stage["facts"] + stage["risks"] + stage["conflicts"]:
        assert item in markdown


@pytest.mark.parametrize("role,collection,risk_key", [
    ("growth-metrics", "metrics", "data_risks"),
    ("growth-sql", "queries", "risks"),
])
def test_role_risks_are_included_in_the_confirmation_summary(role: str, collection: str, risk_key: str) -> None:
    stage = stage_output(role, "readability-run", "s01-stage", 1)
    stage["risks"] = ["共同风险：统计范围尚未确认。"]
    stage["role_payload"][collection][0][risk_key] = ["该指标缺少完整观察窗口。"]
    markdown = render_markdown(stage)
    risk_section = markdown.split("## 需要注意的地方", 1)[1].split("## 需要你确认的内容", 1)[0]

    assert "共同风险：统计范围尚未确认。" in risk_section
    assert "该指标缺少完整观察窗口。" in risk_section
    assert "未列出新增风险" not in risk_section


def test_nested_hypothesis_is_prose_and_keeps_uncertainty() -> None:
    stage = stage_output("growth-insight", "readability-run", "s04-insight", 1)
    stage["role_payload"]["attribution_hypotheses"] = [{
        "hypothesis": "新用户留存可能下降",
        "confidence": "尚未确认",
        "validation_plan": {"method": "补充注册批次对比", "assumptions": ["观察窗口完整"]},
    }]
    markdown = render_markdown(stage)

    assert "新用户留存可能下降" in markdown
    assert "尚未确认" in markdown
    assert "补充注册批次对比" in markdown
    assert "观察窗口完整" in markdown
    assert "validation_plan" not in markdown
    assert "{'" not in markdown


def test_nested_evidence_is_kept_in_appendix_not_business_body() -> None:
    stage = stage_output("growth-insight", "readability-run", "s04-insight", 1)
    reference = {
        "artifact_id": "result-unique-id",
        "sha256": "d" * 64,
        "selector_type": "csv_cell",
        "selector_value": {"row": 1, "column": "revenue_total"},
    }
    stage["role_payload"]["observations"] = [{
        "observation_id": "obs-1",
        "statement": "本周活跃用户较少，但原因尚未验证。",
        "metric_ids": ["revenue_total"],
        "evidence_refs": [reference],
        "confidence": "low",
    }]
    original = deepcopy(stage)
    markdown = render_markdown(stage)
    body, appendix = markdown.split("## 技术详情", 1)

    assert "本周活跃用户较少，但原因尚未验证。" in body
    assert "可信程度：较低" in body
    assert reference["sha256"] not in body
    assert reference["sha256"] in appendix
    assert reference["artifact_id"] in appendix
    assert stage == original


def test_stage_field_evidence_uses_readable_location_labels() -> None:
    stage = stage_output("growth-review", "readability-run", "s06-review", 1)
    stage["evidence"] = [{
        "artifact_id": "insight-result",
        "sha256": "e" * 64,
        "selector_type": "stage_field",
        "selector_value": "/role_payload/observations/0/statement",
    }, {
        "artifact_id": "calculated-result",
        "sha256": "f" * 64,
        "selector_type": "calculation_id",
        "selector_value": "daily-total",
    }]
    original = deepcopy(stage)
    markdown = render_markdown(stage)

    assert "本阶段内容 / 观察结论 / 索引 0 / 结论" in markdown
    assert "role_payload" not in markdown
    assert "stage_field" not in markdown
    assert "定位方式：已登记的计算" in markdown
    assert "calculation_id" not in markdown
    assert stage == original


def test_final_report_preserves_agent_caveats_risks_and_actions() -> None:
    stage = stage_output("growth-report", "readability-run", "s07-report", 1)
    stage["role_payload"].update(
        executive_summary="数据不足以证明活跃度下降。",
        caveats=["当前只覆盖一个自然日。"],
        next_steps=["补充两周行为数据后再比较。"],
    )
    stage["facts"] = ["当前有100名用户。"]
    stage["risks"] = ["尚未排除测试账号。"]
    markdown = render_markdown(stage)

    for text in ("数据不足以证明活跃度下降。", "当前只覆盖一个自然日。", "补充两周行为数据后再比较。", "当前有100名用户。", "尚未排除测试账号。"):
        assert text in markdown
    assert "业务背景" not in markdown
    assert "指标冲突检查" not in markdown


def test_accidental_model_code_formatting_is_not_rendered_or_mutated() -> None:
    stage = stage_output("growth-business", "readability-run", "s01-business", 1)
    stage["summary"] = "请确认本次的`观察范围`。"
    stage["role_payload"]["business_context"] = "业务说明。\n```json\n{\"internal_key\": 1}\n```\n补充说明。"
    original = deepcopy(stage)
    markdown = render_markdown(stage)

    assert "请确认本次的观察范围。" in markdown
    assert "业务说明。" in markdown
    assert "补充说明。" in markdown
    assert "internal_key" not in markdown
    assert "`" not in markdown
    assert stage == original
