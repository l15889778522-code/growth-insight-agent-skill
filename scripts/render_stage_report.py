#!/usr/bin/env python3
"""Render a validated stage JSON object as a plain-language Markdown report."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

from runtime_common import atomic_write_text, load_json
from validate_stage_output import validate_stage


ROLE_LABELS = {
    "growth-business": "业务理解",
    "growth-metrics": "指标体系",
    "growth-sql": "数据查询方案",
    "growth-insight": "洞察归因",
    "growth-visualization": "可视化",
    "growth-review": "质量评审",
    "growth-report": "最终报告",
}

STATUS_LABELS = {
    "PASS": "通过",
    "PASS_WITH_RISKS": "有风险地通过",
    "BLOCKED": "等待补充信息",
    "FAIL": "未通过",
}

METRIC_TYPE_LABELS = {
    "north-star": "北极星指标",
    "core": "核心指标",
    "process": "过程指标",
    "guardrail": "护栏指标",
    "diagnostic": "诊断指标",
}

METRIC_STATUS_LABELS = {
    "exploratory": "探索中",
    "candidate": "候选指标",
    "official": "正式指标",
}

CONFIDENCE_LABELS = {"low": "较低", "medium": "中等", "high": "较高"}

CHART_TYPE_LABELS = {"line": "折线图", "bar": "柱状图", "scatter": "散点图"}

CHART_STATUS_LABELS = {
    "ready": "可以生成",
    "blocked_by_missing_data": "因缺少数据暂时无法生成",
}

FIELD_LABELS = {
    "statement": "结论",
    "summary": "说明",
    "text": "内容",
    "name": "名称",
    "question": "需要回答的问题",
    "value": "结果",
    "metric_id": "指标编号",
    "metric_ids": "关联指标",
    "confidence": "可信程度",
    "reason": "原因",
    "impact": "影响",
    "recommendation": "建议",
    "priority": "优先级",
    "status": "状态",
    "severity": "严重程度",
    "responsible_stage": "责任阶段",
    "field": "字段",
    "op": "条件",
    "calculation_id": "计算编号",
    "expression": "计算方式",
    "output_value": "计算结果",
    "precision": "精度说明",
    "row": "数据行号",
    "column": "数据列",
    "hypothesis": "可能原因",
    "rationale": "判断依据",
    "supporting_evidence": "支持这一判断的证据",
    "counter_evidence": "反向证据",
    "validation": "验证方法",
    "validation_steps": "验证步骤",
    "next_steps": "下一步行动",
    "assumptions": "分析前提",
    "limitations": "限制",
    "evidence_refs": "相关证据",
    "input_evidence_refs": "计算依据",
    "source_fields": "来源字段",
    "result_column": "结果列",
    "query_id": "查询编号",
    "query_ids": "查询编号",
    "chart_id": "图表编号",
    "chart_ids": "图表编号",
    "breaks": "缺少的关联",
    "warning": "提醒",
    "confidence_notes": "可信程度说明",
    "expected_impact": "预期影响",
    "test": "验证方法",
    "method": "方法",
    "validation_plan": "验证安排",
    "calculation_grain": "计算粒度",
    "denominator_scope": "比较基准范围",
    "exclusions": "排除条件",
    "role_payload": "本阶段内容",
    "observations": "观察结论",
    "recommendations": "建议",
    "findings": "复核问题",
    "metrics": "指标",
    "queries": "查询",
    "chart_specs": "图表计划",
    "facts": "事实",
    "calculations": "计算结果",
}

SELECTOR_LABELS = {
    "file": "整个文件",
    "json_pointer": "结构化记录中的指定位置",
    "csv_row": "数据表中的指定行",
    "csv_cell": "数据表中的指定单元格",
    "stage_field": "阶段记录中的指定内容",
    "query_manifest_field": "查询记录中的指定内容",
    "calculation_id": "已登记的计算",
}


def _has_value(value: Any) -> bool:
    return value not in (None, "", [], {})


def _label(key: str) -> str:
    return FIELD_LABELS.get(key, key if re.search("[\u4e00-\u9fff]", key) else "补充信息")


def _stage_label(value: str) -> str:
    if value in ROLE_LABELS:
        return ROLE_LABELS[value]
    for role, label in ROLE_LABELS.items():
        if re.fullmatch(r"s\d+-" + role.removeprefix("growth-"), value):
            return label
    return value


def _finish_report(lines: list[str]) -> str:
    # Keep accidental model-supplied code formatting out of the reader's report.
    text = "\n".join(lines)
    text = re.sub(
        r"(?ms)^[ \t]*(?P<fence>`{3,}|~{3,})[^\n]*\n.*?^[ \t]*(?P=fence)[ \t]*$",
        "技术内容保留在本阶段原始记录中，此处不展开。",
        text,
    )
    text = re.sub(r"(?m)^[ \t]*(?:`{3,}|~{3,})[^\n]*$", "", text)
    text = text.replace("`", "")
    return re.sub(r"(?m)^(?: {4,}|\t+)(?=\S)", "", text)


def _plain_scalar(value: Any) -> str:
    if value is None:
        return "无"
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def _plain_value(value: Any) -> str:
    if isinstance(value, dict):
        if {"artifact_id", "sha256", "selector_type", "selector_value"} <= set(value):
            return _evidence_text(value)
        parts = [
            f"{_label(str(key))}：{_plain_value(item)}"
            for key, item in value.items()
            if _has_value(item)
        ]
        return "；".join(parts) if parts else "无"
    if isinstance(value, list):
        return "；".join(_plain_value(item) for item in value) if value else "无"
    return _plain_scalar(value)


def _plain_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        primary = next(
            (key for key in ("statement", "summary", "text", "name", "question", "value", "warning") if _has_value(value.get(key))),
            None,
        )
        parts = [_plain_value(value[primary])] if primary else []
        for key, item in value.items():
            if key == primary or not _has_value(item):
                continue
            if key.endswith(("_id", "_ids", "_refs", "sha256")) or key in {"path", "evidence", "selector_type", "selector_value"}:
                continue
            parts.append(f"{_label(key)}：{_plain_text(item)}")
        return "；".join(parts)
    if isinstance(value, list):
        return "；".join(_plain_text(item) for item in value)
    return _plain_value(value)


def _plain_list(values: Any, limit: int | None = None) -> list[str]:
    if not isinstance(values, list):
        return []
    selected = values if limit is None else values[:limit]
    rendered = [_plain_text(item) for item in selected if _has_value(item)]
    return [item for item in rendered if item]


def _evidence_text(reference: dict[str, Any]) -> str:
    artifact_id = reference.get("artifact_id", "未知证据")
    digest = reference.get("sha256") or "未提供"
    selector_type = SELECTOR_LABELS.get(reference.get("selector_type"), "指定位置")
    selector_value = reference.get("selector_value")
    location = ""
    if _has_value(selector_value):
        if isinstance(selector_value, str) and selector_value.startswith("/"):
            tokens = [token.replace("~1", "/").replace("~0", "~") for token in selector_value.split("/")[1:]]
            position = " / ".join(f"索引 {token}" if token.isdigit() else _label(token) for token in tokens)
        else:
            position = _plain_value(selector_value)
        location = f"，定位信息：{position}"
    return f"证据编号：{artifact_id}；完整性校验码：{digest}；定位方式：{selector_type}{location}"


def _artifact_text(artifact: dict[str, Any]) -> str:
    return (
        f"产物编号：{artifact.get('artifact_id', '未知')}；"
        f"类型：{artifact.get('kind', '未知')}；"
        f"文件位置：{artifact.get('path', '未提供')}；"
        f"完整性校验码：{artifact.get('sha256') or '未提供'}"
    )


def _append_bullets(lines: list[str], values: Any, empty_text: str | None = None) -> None:
    items = _plain_list(values)
    if items:
        lines.extend(f"- {item}" for item in items)
    elif empty_text:
        lines.append(empty_text)


def _append_labeled_list(lines: list[str], label: str, values: Any, empty_text: str = "无") -> None:
    items = _plain_list(values)
    lines.append(f"- {label}：{'；'.join(items) if items else empty_text}")


def _business_implication(stage: dict[str, Any]) -> str:
    role = stage.get("role")
    payload = stage.get("role_payload", {})
    if role == "growth-business":
        return str(payload.get("decision_question") or "本阶段明确要解决的业务问题和分析范围。")
    if role == "growth-metrics":
        count = len(payload.get("metrics", [])) if isinstance(payload.get("metrics"), list) else 0
        return f"本阶段建议关注 {count} 个指标，用来把业务问题转成可以计算和比较的答案。"
    if role == "growth-sql":
        count = len(payload.get("queries", [])) if isinstance(payload.get("queries"), list) else 0
        return f"本阶段设计了 {count} 个查询方案。执行前仍需通过只读校验，并获得你对具体查询的独立批准。"
    if role == "growth-insight":
        return "本阶段把数据反映的事实、可能原因和仍需验证的判断分开说明。"
    if role == "growth-visualization":
        count = len(payload.get("chart_specs", [])) if isinstance(payload.get("chart_specs"), list) else 0
        return f"本阶段规划了 {count} 张图，帮助快速理解数据变化；图表不会替代数据质量检查。"
    if role == "growth-review":
        decision = payload.get("decision") or stage.get("stage_status")
        return {
            "PASS": "该角色认为这份分析通过了质量检查，可以提交给你确认。",
            "PASS_WITH_RISKS": "这份分析可以在明确风险的前提下使用，但仍有需要留意的限制。",
            "FAIL": "这份分析没有通过质量检查，需要先回到指定阶段修正。",
        }.get(decision, "本阶段检查这份分析是否足够可靠。")
    return "本阶段汇总已经确认的结果、限制和下一步行动。"


def _role_findings(stage: dict[str, Any]) -> list[str]:
    payload = stage.get("role_payload", {})
    facts = _plain_list(stage.get("facts"))
    if facts:
        return facts
    role = stage.get("role")
    if role == "growth-business":
        return _plain_list([payload.get("decision_question")], 5)
    if role == "growth-metrics":
        return [
            f"建议使用“{item.get('name', '未命名指标')}”回答当前业务问题。"
            for item in payload.get("metrics", [])[:5]
        ]
    if role == "growth-sql":
        return [str(item.get("purpose")) for item in payload.get("queries", [])[:5] if item.get("purpose")]
    if role == "growth-insight":
        return [
            str(item.get("statement"))
            for item in payload.get("observations", [])[:5]
            if item.get("statement")
        ]
    if role == "growth-visualization":
        return [str(item.get("question")) for item in payload.get("chart_specs", [])[:5] if item.get("question")]
    if role == "growth-review":
        findings = [str(item.get("summary")) for item in payload.get("findings", [])[:5] if item.get("summary")]
        return findings or [_business_implication(stage)]
    return _plain_list(payload.get("evidence_summary"), 5)


def _risk_items(stage: dict[str, Any]) -> list[str]:
    payload = stage.get("role_payload", {})
    items = _plain_list(stage.get("risks"))
    items.extend(_plain_list(stage.get("conflicts")))
    role = stage.get("role")
    role_risks = {
        "growth-metrics": payload.get("dependency_gaps", []),
        "growth-sql": payload.get("unsupported_metrics", []),
        "growth-insight": payload.get("confidence_notes", []),
        "growth-review": payload.get("required_fixes", []) + payload.get("lineage_breaks", []),
        "growth-report": payload.get("caveats", []),
    }
    items.extend(_plain_list(role_risks.get(role, [])))
    for metric in payload.get("metrics", []):
        items.extend(_plain_list(metric.get("data_risks")))
    for query in payload.get("queries", []):
        items.extend(_plain_list(query.get("risks")))
    for finding in payload.get("findings", []):
        if finding.get("summary"):
            items.append(str(finding["summary"]))
    for warning in payload.get("data_quality_warnings", []):
        if warning.get("warning"):
            items.append(str(warning["warning"]))
    for chart in payload.get("chart_specs", []):
        if chart.get("status") == "blocked_by_missing_data":
            items.append(f"“{chart.get('title', '图表')}”因缺少数据暂时无法生成。")
    return list(dict.fromkeys(items))


def _render_business(payload: dict[str, Any]) -> list[str]:
    lines = ["### 业务背景", "", str(payload.get("business_context", "未说明")), "", "### 要回答的问题", ""]
    lines.extend([str(payload.get("decision_question", "未说明")), "", "### 分析范围", ""])
    _append_bullets(lines, payload.get("scope"), "本阶段没有新增分析范围。")
    lines.extend(["", "### 本次不分析的内容", ""])
    _append_bullets(lines, payload.get("non_goals"), "本阶段没有明确排除项。")
    lines.extend(["", "### 建议观察的分类维度", ""])
    _append_bullets(lines, payload.get("dimensions"), "本阶段没有新增分类维度。")
    lines.extend(["", "### 仍需确认的问题", ""])
    _append_bullets(lines, payload.get("open_questions"), "当前没有新增问题。")
    return lines


def _render_metrics(payload: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    metrics = payload.get("metrics", [])
    for index, metric in enumerate(metrics, start=1):
        lines.extend([f"### {index}. {metric.get('name', '未命名指标')}", ""])
        lines.append(f"- 指标类型：{METRIC_TYPE_LABELS.get(metric.get('type'), metric.get('type', '未说明'))}")
        lines.append(f"- 它代表什么：{metric.get('business_definition', '未说明')}")
        lines.append(f"- 如何计算：{metric.get('formula', '未说明')}")
        lines.append(f"- 统计粒度：{metric.get('grain', '未说明')}")
        lines.append(f"- 时间范围：{metric.get('time_window', '未说明')}")
        _append_labeled_list(lines, "建议分类观察", metric.get("dimensions"), "不拆分")
        lines.append(f"- 当前状态：{METRIC_STATUS_LABELS.get(metric.get('status'), metric.get('status', '未说明'))}")
        lines.append(f"- 负责人：{metric.get('owner', '未说明')}")
        _append_labeled_list(lines, "主要数据风险", metric.get("data_risks"), "本阶段未列出")
        lines.append("")
    lines.extend(["### 数据依赖缺口", ""])
    _append_bullets(lines, payload.get("dependency_gaps"), "本阶段未列出数据依赖缺口。")
    lines.extend(["", "### 指标冲突检查", ""])
    _append_bullets(lines, payload.get("conflict_checks"), "尚未提供冲突检查结论。")
    return lines


def _render_sql(payload: dict[str, Any]) -> list[str]:
    lines = ["### 查询环境", "", f"计划使用的数据查询语言：{payload.get('dialect', '未说明')}。", ""]
    queries = payload.get("queries", [])
    for index, query in enumerate(queries, start=1):
        lines.extend([f"### {index}. {query.get('purpose', '未说明查询目的')}", ""])
        _append_labeled_list(lines, "关联指标", query.get("metric_ids"), "未绑定指标")
        lines.append(f"- 是否具备执行条件：{'是' if query.get('executable') else '否'}")
        _append_labeled_list(lines, "执行前需要注意", query.get("risks"), "本阶段未列出新增风险")
        for mapping in payload.get("field_mappings", []):
            if mapping.get("metric_id") not in query.get("metric_ids", []):
                continue
            if mapping.get("calculation_grain"):
                lines.append(f"- 计算粒度：{mapping['calculation_grain']}")
            if mapping.get("denominator_scope"):
                lines.append(f"- 比较基准包含哪些人或记录：{mapping['denominator_scope']}")
            if mapping.get("exclusions"):
                _append_labeled_list(lines, "需要排除的记录", mapping["exclusions"])
        lines.append("")
    lines.extend(["### 暂不支持的指标", ""])
    _append_bullets(lines, payload.get("unsupported_metrics"), "本阶段未列出不支持的指标。")
    return lines


def _render_insight(payload: dict[str, Any]) -> list[str]:
    lines = ["### 数据已经说明的事实", ""]
    observations = payload.get("observations", [])
    if observations:
        for item in observations:
            confidence = CONFIDENCE_LABELS.get(item.get("confidence"), item.get("confidence", "未说明"))
            metrics = "、".join(item.get("metric_ids", [])) or "未绑定指标"
            lines.append(f"- {item.get('statement', '未说明')}（可信程度：{confidence}；关联指标：{metrics}）")
    else:
        lines.append("当前数据还没有形成可以直接引用的事实。")
    lines.extend(["", "### 可能原因", ""])
    _append_bullets(lines, payload.get("attribution_hypotheses"), "当前没有提出新的原因假设。")
    lines.extend(["", "### 与结论不一致的证据", ""])
    _append_bullets(lines, payload.get("counter_evidence"), "本阶段未提供反向证据。")
    lines.extend(["", "### 建议如何继续验证", ""])
    _append_bullets(lines, payload.get("validation_steps"), "当前没有新增验证步骤。")
    lines.extend(["", "### 行动建议", ""])
    recommendations = payload.get("recommendations", [])
    _append_bullets(lines, [item.get("text") for item in recommendations], "当前没有新增行动建议。")
    lines.extend(["", "### 可信程度说明", ""])
    _append_bullets(lines, payload.get("confidence_notes"), "当前没有新增说明。")
    return lines


def _render_visualization(payload: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    charts = payload.get("chart_specs", [])
    for index, chart in enumerate(charts, start=1):
        lines.extend([f"### {index}. {chart.get('title', '未命名图表')}", ""])
        lines.append(f"- 这张图要回答：{chart.get('question', '未说明')}")
        lines.append(f"- 图表形式：{CHART_TYPE_LABELS.get(chart.get('type'), chart.get('type', '未说明'))}")
        lines.append(f"- 观察指标：{chart.get('metric_id', '未说明')}")
        lines.append(f"- 当前状态：{CHART_STATUS_LABELS.get(chart.get('status'), chart.get('status', '未说明'))}")
        _append_labeled_list(lines, "筛选条件", chart.get("filters"), "不额外筛选")
        lines.append("")
    lines.extend(["### 建议阅读顺序", ""])
    titles = {chart["chart_id"]: chart["title"] for chart in charts}
    _append_bullets(lines, [titles.get(item, item) for item in payload.get("reading_order", [])], "当前没有指定阅读顺序。")
    return lines


def _render_review(payload: dict[str, Any]) -> list[str]:
    decision = STATUS_LABELS.get(payload.get("decision"), payload.get("decision", "未说明"))
    lines = ["### 评审结论", "", f"这份分析的评审结果是：{decision}。", "", "### 发现的问题", ""]
    findings = payload.get("findings", [])
    if findings:
        for item in findings:
            responsible = _stage_label(item.get("responsible_stage") or "尚未定位")
            severity = {
                "P0": "紧急，必须先修复",
                "P1": "高，必须先修复",
                "P2": "中，需要明确风险",
                "P3": "低，可后续改善",
            }.get(item.get("severity"), "未说明")
            lines.append(
                f"- {item.get('summary', '未说明')}（严重程度：{severity}；责任阶段：{responsible}）"
            )
    else:
        lines.append("本阶段未登记具体问题。")
    lines.extend(["", "### 必须完成的修正", ""])
    _append_bullets(lines, payload.get("required_fixes"), "当前没有必须修正的内容。")
    lines.extend(["", "### 可以后续改善的内容", ""])
    _append_bullets(lines, payload.get("optional_improvements"), "当前没有额外改善建议。")
    lines.extend(["", "### 证据链检查", ""])
    _append_bullets(lines, payload.get("lineage_breaks"), "本阶段未列出指标、查询、结果和结论之间的证据链断点。")
    lines.extend(["", "### 数据质量提醒", ""])
    _append_bullets(lines, [item.get("warning") for item in payload.get("data_quality_warnings", [])], "本阶段未列出新的数据质量提醒。")
    if payload.get("rollback_stage"):
        lines.extend(["", f"需要回退到的阶段：{_stage_label(payload['rollback_stage'])}。"])
    return lines


def _render_role_content(stage: dict[str, Any]) -> list[str]:
    payload = stage.get("role_payload", {})
    renderers = {
        "growth-business": _render_business,
        "growth-metrics": _render_metrics,
        "growth-sql": _render_sql,
        "growth-insight": _render_insight,
        "growth-visualization": _render_visualization,
        "growth-review": _render_review,
    }
    renderer = renderers.get(stage.get("role"))
    return renderer(payload) if renderer else []


def _render_common_agent_content(stage: dict[str, Any]) -> list[str]:
    sections = (
        ("已确认的内容", stage.get("confirmed_decisions")),
        ("分析前提", stage.get("assumptions")),
        ("仍需验证的判断", stage.get("hypotheses")),
        ("尚未解决的问题", stage.get("open_questions")),
    )
    lines: list[str] = []
    for title, values in sections:
        if not _has_value(values):
            continue
        lines.extend([f"## {title}", ""])
        _append_bullets(lines, values)
        lines.append("")
    calculations = stage.get("calculations", [])
    if calculations:
        lines.extend(["## 计算说明", ""])
        for item in calculations:
            if not isinstance(item, dict):
                lines.extend([_plain_text(item), ""])
                continue
            lines.extend([
                f"- 计算方式：{item.get('expression', '未说明')}",
                f"- 计算结果：{_plain_value(item.get('output_value'))}",
                f"- 精度说明：{item.get('precision', '未说明')}",
                "",
            ])
    return lines


def _render_confirmation(stage: dict[str, Any]) -> list[str]:
    status = STATUS_LABELS.get(stage.get("stage_status"), stage.get("stage_status", "未知"))
    next_stage = stage.get("recommended_next_stage")
    next_label = _stage_label(str(next_stage)) if next_stage else "未建议后续角色"
    lines = [f"- 当前结果：{status}。", f"- 该角色建议的下一步：{next_label}。实际安排以已批准路线为准。"]
    required = _plain_list(stage.get("required_next_inputs"))
    if required:
        lines.append(f"- 继续前需要补充：{'；'.join(required)}")
    if stage.get("stage_status") in {"PASS", "PASS_WITH_RISKS"}:
        lines.append("- 你可以确认本阶段、提出修改，或要求回退；本报告本身不代表已经获得批准。")
    else:
        lines.append("- 本阶段暂不能批准。请先补充信息或修正问题，再重新提交确认。")
    return lines


def _all_evidence(value: Any) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if {"artifact_id", "sha256", "selector_type", "selector_value"} <= set(value):
            return [value]
        for item in value.values():
            references.extend(_all_evidence(item))
    elif isinstance(value, list):
        for item in value:
            references.extend(_all_evidence(item))
    return references


def _render_technical_details(stage: dict[str, Any]) -> list[str]:
    role = stage.get("role")
    payload = stage.get("role_payload", {})
    lines = [
        "## 技术详情",
        "",
        "以下信息用于恢复运行和核对证据，不影响前面的业务阅读。",
        "",
        "| 项目 | 内容 |",
        "|---|---|",
        f"| 运行编号 | {stage.get('run_id', '未提供')} |",
        f"| 阶段编号 | {stage.get('stage_id', '未提供')} |",
        f"| 执行角色 | {ROLE_LABELS.get(role, role)} |",
        f"| 本阶段尝试次数 | {stage.get('attempt', '未提供')} |",
        f"| 合同版本 | {stage.get('agent_contract_version', '未提供')} |",
        "",
    ]
    if role == "growth-metrics":
        lines.extend(["### 指标内部记录", ""])
        for metric in payload.get("metrics", []):
            dependencies = "、".join(metric.get("field_dependencies", [])) or "未提供"
            lines.append(
                f"- {metric.get('name', '未命名指标')}：指标编号 {metric.get('metric_id', '未提供')}，"
                f"版本 {metric.get('version', '未提供')}，数据来源 {metric.get('source', '未提供')}，"
                f"依赖字段 {dependencies}。"
            )
        lines.append("")
    elif role == "growth-sql":
        lines.extend(["### 查询内部记录", ""])
        for query in payload.get("queries", []):
            metrics = "、".join(query.get("metric_ids", [])) or "未绑定指标"
            lines.append(
                f"- 查询编号 {query.get('sql_id', '未提供')}，关联指标 {metrics}。"
                "具体查询语句保存在本阶段结构化原始记录中，文字报告不展开。"
            )
        for mapping in payload.get("field_mappings", []):
            fields = "、".join(mapping.get("source_fields", [])) or "未提供"
            lines.append(
                f"- 指标 {mapping.get('metric_id', '未提供')} 使用字段 {fields}，"
                f"结果列为 {mapping.get('result_column') or '未提供'}，"
                f"当前{'支持' if mapping.get('supported') else '不支持'}计算。"
            )
        lines.append("")
    elif role == "growth-visualization":
        lines.extend(
            [
                "### 图表数据来源",
                "",
                f"- 来源文件：{payload.get('source_file') or '未提供'}",
                f"- 完整性校验码：{payload.get('source_sha256') or '未提供'}",
                "",
            ]
        )
        for chart in payload.get("chart_specs", []):
            aggregation = {"none": "不额外聚合", "sum": "求和", "mean": "求平均", "count": "计数"}.get(chart.get("aggregation"), "未说明")
            formats = "、".join({"png": "图片", "html": "网页"}.get(item, item) for item in chart.get("output_formats", []))
            lines.append(
                f"- {chart.get('title', '未命名图表')}：图表编号 {chart.get('chart_id', '未提供')}，"
                f"横轴 {chart.get('x', '未提供')}，纵轴 {chart.get('y', '未提供')}，"
                f"汇总方式为{aggregation}，交付形式为{formats}。"
            )
        lines.append("")

    evidence: list[Any] = list(stage.get("evidence", []))
    for reference in _all_evidence(stage):
        if reference not in evidence:
            evidence.append(reference)
    if evidence:
        lines.extend(["### 证据索引", ""])
        lines.extend(f"- {_evidence_text(item) if isinstance(item, dict) else _plain_text(item)}" for item in evidence)
        lines.append("")
    artifacts = stage.get("data_artifacts", [])
    if artifacts:
        lines.extend(["### 数据产物", ""])
        lines.extend(f"- {_artifact_text(item) if isinstance(item, dict) else _plain_text(item)}" for item in artifacts)
        lines.append("")
    lineage = stage.get("lineage", [])
    if lineage:
        lines.extend(["### 证据关系记录", ""])
        _append_bullets(lines, [_plain_value(item) for item in lineage])
        lines.append("")
    for key, id_key, title in (
        ("observations", "observation_id", "结论对应的证据"),
        ("findings", "finding_id", "复核问题对应的证据"),
        ("recommendations", "recommendation_id", "建议对应的证据"),
    ):
        items = payload.get(key, [])
        if not items:
            continue
        lines.extend([f"### {title}", ""])
        for item in items:
            lines.extend([f"记录编号：{item.get(id_key, '未提供')}。", ""])
            refs = item.get("evidence_refs", [])
            if refs:
                lines.extend(f"- {_evidence_text(ref) if isinstance(ref, dict) else _plain_text(ref)}" for ref in refs)
            else:
                lines.append("未绑定证据。")
            lines.append("")
    return lines


def _render_final_report(stage: dict[str, Any]) -> str:
    payload = stage["role_payload"]
    lines = [
        "# 数据分析报告",
        "",
        "这是一份面向业务阅读的分析总结。运行记录和完整证据保留在文末技术详情中。",
        "",
        "## 一句话结论",
        "",
        payload["executive_summary"],
        "",
        "## 这对你的业务意味着什么",
        "",
        "以下结论只适用于本次确认的数据范围和指标口径，不能自动解释为因果关系。",
        "",
        "## 证据摘要",
        "",
    ]
    _append_bullets(lines, payload.get("evidence_summary"), "当前没有可展示的证据摘要。")
    lines.extend(["", "## 需要注意的地方", ""])
    risks = list(dict.fromkeys(_plain_list(payload.get("caveats")) + _risk_items(stage)))
    _append_bullets(lines, risks, "本阶段未列出新的限制。")
    lines.extend(["", "## 详细建议", ""])
    recommendations = payload.get("recommendations", [])
    if recommendations:
        for index, item in enumerate(recommendations, start=1):
            lines.extend([f"### {index}. {item.get('text', '未说明')}", ""])
            metrics = "、".join(item.get("metric_ids", [])) or "未绑定指标"
            lines.extend([f"关联指标：{metrics}。", ""])
    else:
        lines.extend(["当前没有新增建议。", ""])
    lines.extend(["## 后续行动", ""])
    _append_bullets(lines, payload.get("next_steps"), "当前没有安排新的后续行动。")
    lines.append("")
    if stage.get("facts"):
        lines.extend(["## 补充事实", ""])
        _append_bullets(lines, stage["facts"])
        lines.append("")
    lines.extend(["## 需要你确认的内容", ""])
    lines.extend(_render_confirmation(stage))
    lines.append("")
    lines.extend(_render_common_agent_content(stage))
    lines.extend(_render_technical_details(stage))
    return _finish_report(lines)


def render_markdown(stage: dict[str, Any]) -> str:
    role = stage["role"]
    if role == "growth-report":
        return _render_final_report(stage)

    lines = [
        f"# {ROLE_LABELS.get(role, role)}报告",
        "",
        "## 一句话结论",
        "",
        stage["summary"],
        "",
        "## 这对你的业务意味着什么",
        "",
        _business_implication(stage),
        "",
        "## 本阶段完成了什么",
        "",
    ]
    lines.extend(_render_role_content(stage))
    lines.extend(["", "## 关键发现", ""])
    _append_bullets(lines, _role_findings(stage), "本阶段还没有形成可以直接引用的结论。")
    lines.extend(["", "## 需要注意的地方", ""])
    _append_bullets(lines, _risk_items(stage), "本阶段未列出新增风险。")
    lines.extend(["", "## 需要你确认的内容", ""])
    lines.extend(_render_confirmation(stage))
    lines.append("")
    lines.extend(_render_common_agent_content(stage))
    lines.extend(_render_technical_details(stage))
    return _finish_report(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Render validated stage JSON as Markdown.")
    parser.add_argument("stage_json", type=Path)
    parser.add_argument("output_markdown", type=Path)
    parser.add_argument("--final-output", type=Path)
    args = parser.parse_args()

    stage = load_json(args.stage_json)
    if not isinstance(stage, dict):
        raise SystemExit("Stage JSON must contain an object.")
    errors = validate_stage(stage, str(stage.get("role", "")))
    if errors:
        raise SystemExit("Stage JSON is invalid:\n- " + "\n- ".join(errors))

    markdown = render_markdown(stage)
    atomic_write_text(args.output_markdown, markdown)
    if args.final_output:
        if stage.get("role") != "growth-report":
            raise SystemExit("--final-output is only valid for growth-report.")
        atomic_write_text(args.final_output, markdown)
    print(args.output_markdown.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
