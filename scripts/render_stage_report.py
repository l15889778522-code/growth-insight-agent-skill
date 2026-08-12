#!/usr/bin/env python3
"""Render a validated stage JSON object into deterministic Markdown."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from evidence import format_evidence_reference
from runtime_common import atomic_write_text, load_json
from validate_stage_output import validate_stage


ROLE_LABELS = {
    "growth-business": "业务理解",
    "growth-metrics": "指标体系",
    "growth-sql": "SQL 分析",
    "growth-insight": "洞察归因",
    "growth-visualization": "可视化",
    "growth-review": "质量评审",
    "growth-report": "最终报告",
}

SECTION_LABELS = {
    "confirmed_decisions": "已确认决策",
    "conflicts": "冲突",
    "facts": "事实",
    "calculations": "计算结果",
    "assumptions": "假设",
    "hypotheses": "待验证推断",
    "evidence": "证据",
    "metrics": "指标",
    "data_artifacts": "数据产物",
    "risks": "风险",
    "open_questions": "开放问题",
    "required_next_inputs": "下一阶段必需输入",
    "lineage": "血缘",
}


def _render_item(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict) and {"artifact_id", "sha256", "selector_type", "selector_value"} <= set(item):
        return format_evidence_reference(item)
    return "```json\n" + json.dumps(item, ensure_ascii=False, indent=2, sort_keys=True) + "\n```"


def _render_section(title: str, value: Any) -> list[str]:
    lines = [f"## {title}", ""]
    if value in (None, [], {}):
        return lines + ["无。", ""]
    if isinstance(value, list):
        for item in value:
            rendered = _render_item(item)
            if rendered.startswith("```"):
                lines.extend([rendered, ""])
            else:
                lines.append(f"- {rendered}")
        lines.append("")
        return lines
    if isinstance(value, dict):
        lines.extend(["```json", json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), "```", ""])
        return lines
    return lines + [str(value), ""]


def _render_final_report(stage: dict[str, Any]) -> str:
    payload = stage["role_payload"]
    lines = [
        "# 数据分析报告",
        "",
        f"> Run `{stage['run_id']}` | Review-approved synthesis | Contract `{stage['agent_contract_version']}`",
        "",
        "## 执行摘要",
        "",
        payload["executive_summary"],
        "",
    ]
    lines.extend(_render_section("证据摘要", payload["evidence_summary"]))
    lines.extend(["## 建议", ""])
    if payload["recommendations"]:
        lines.extend(["| ID | 建议 | 指标 | 证据 |", "|---|---|---|---|"])
        for item in payload["recommendations"]:
            metric_ids = ", ".join(item["metric_ids"]) or "-"
            evidence_refs = ", ".join(format_evidence_reference(ref) for ref in item["evidence_refs"]) or "-"
            evidence_refs = evidence_refs.replace("|", "\\|").replace("\n", " ")
            text = item["text"].replace("|", "\\|")
            lines.append(f"| `{item['recommendation_id']}` | {text} | {metric_ids} | {evidence_refs} |")
        lines.append("")
    else:
        lines.extend(["无。", ""])
    lines.extend(_render_section("限制与风险", payload["caveats"]))
    lines.extend(_render_section("后续行动", payload["next_steps"]))
    lines.extend(["## 可审计附录", ""])
    for key in ("facts", "calculations", "assumptions", "hypotheses", "evidence", "data_artifacts", "lineage"):
        lines.extend(_render_section(SECTION_LABELS[key], stage.get(key)))
    return "\n".join(lines)


def render_markdown(stage: dict[str, Any]) -> str:
    role = stage["role"]
    if role == "growth-report":
        return _render_final_report(stage)
    lines = [
        f"# {ROLE_LABELS.get(role, role)}报告",
        "",
        "| 字段 | 值 |",
        "|---|---|",
        f"| run_id | `{stage['run_id']}` |",
        f"| stage_id | `{stage['stage_id']}` |",
        f"| role | `{role}` |",
        f"| attempt | `{stage['attempt']}` |",
        f"| stage_status | `{stage['stage_status']}` |",
        f"| agent_contract_version | `{stage['agent_contract_version']}` |",
        "",
        "## 摘要",
        "",
        stage["summary"],
        "",
    ]
    for key, title in SECTION_LABELS.items():
        lines.extend(_render_section(title, stage.get(key)))
    lines.extend(_render_section("角色专属结果", stage.get("role_payload")))
    lines.extend(
        [
            "## Handoff",
            "",
            f"- stage_status: `{stage['stage_status']}`",
            f"- approval_status: `{'awaiting_user_confirmation' if stage['stage_status'] in {'PASS', 'PASS_WITH_RISKS'} else 'not_approvable'}`",
            f"- recommended_next_stage: `{stage.get('recommended_next_stage') or 'END'}`",
            f"- required_next_inputs: `{len(stage.get('required_next_inputs', []))}` item(s)",
            "",
        ]
    )
    return "\n".join(lines)


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
