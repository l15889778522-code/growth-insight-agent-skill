# Handoff Contract

Every stage report must end with this block:

```text
## Handoff
- stage_status: PASS | PASS_WITH_RISKS | BLOCKED | FAIL
- approval_status: awaiting_user_confirmation
- confirmed_decisions:
- assumptions:
- evidence_used:
- open_questions:
- risks:
- required_next_inputs:
- next_agent:
```

Every agent's final chat message must include:

```text
HANDOFF_READY: <absolute output path>
Stage status: <status>
Next agent: <agent name>
Summary: <three to six concise lines>
```

Rules:

- Write the full report to the exact path provided by the orchestrator.
- Use UTF-8 Markdown and Chinese unless the user asks for another language.
- Treat prior reports as the source of confirmed decisions.
- Never silently change a confirmed definition. Record proposed changes as risks or open questions.
- Separate facts, calculated results, assumptions, and hypotheses.
- Never claim a query ran unless execution evidence is available.
- A completed handoff proposes the next agent but does not authorize starting it.
- The root orchestrator must show the report to the user and wait for a new explicit confirmation message.
