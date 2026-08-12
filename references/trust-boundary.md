# Trust Boundary

## Decision

The v1.2 workflow records provenance without claiming guarantees that the Codex host does not expose.

Use these trust levels:

- `self_asserted`: metadata was supplied by the root orchestrator or CLI caller and has no host-verifiable receipt.
- `host_observed`: metadata came from a Codex tool result, such as a spawned Agent ID, but has no portable host signature.
- `host_signed`: reserved for a future host-verifiable receipt. Never emit this value without a verifiable signed object.

Current Codex subagent tools return an Agent ID and expose a visible Agent thread. They do not expose a portable signature that deterministic Skill scripts can verify offline. The current conversation surface likewise does not expose a signed user-message receipt to the Skill runtime.

## Approval Provenance

Every approval records the exact user text, its SHA-256, the interaction surface, capture time, and trust level. In the current runtime, normal approvals are `self_asserted` because `runctl.py` receives them from the root orchestrator.

Do not describe a `self_asserted` approval as proof of user identity. It proves only that the recorded command was hash-bound to the pending route, stage, query, or rollback object.

## Agent Provenance

Every stage attempt records an execution receipt before the stage can be accepted. The receipt binds:

- run, stage, role, and attempt;
- role configuration hash captured when the run was initialized;
- Agent ID or thread ID returned by the host when available;
- raw response path and SHA-256;
- parsed stage JSON SHA-256;
- timestamps, resolved model, and Token metadata when available;
- capture method and trust level.

Use `host_observed` when the Agent ID came directly from the native spawn result. Use `self_asserted` only for compatibility imports, and make that downgrade visible in audit and summaries.

The receipt does not prove that the host loaded a particular installed TOML unless a future Codex receipt exposes that fact. The runtime does prevent the Skill-side role configuration from changing between run initialization, stage start, and receipt capture.

## Compatibility

New runs use contract version `1.2`. Version `1.1` runs remain readable and auditable. Mutation requires explicit migration so a legacy run is never silently upgraded while an approval or stage is pending.

The v1.1 golden fixture under `tests/fixtures/v1.1-golden-run/` is the compatibility baseline. Migration must preserve its request hash, event order, state status, and artifact hashes.
