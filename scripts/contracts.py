"""Shared contract constants for the multi-agent data analysis runtime."""

from __future__ import annotations

import re
from pathlib import Path


VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
CURRENT_CONTRACT_VERSION = VERSION_FILE.read_text(encoding="ascii").strip()
if not re.fullmatch(r"[0-9]+\.[0-9]+", CURRENT_CONTRACT_VERSION):
    raise RuntimeError(f"Invalid contract version in {VERSION_FILE}: {CURRENT_CONTRACT_VERSION!r}")
LEGACY_CONTRACT_VERSIONS = ("1.1",)
SUPPORTED_CONTRACT_VERSIONS = (*LEGACY_CONTRACT_VERSIONS, CURRENT_CONTRACT_VERSION)

ROLES = (
    "growth-business",
    "growth-metrics",
    "growth-sql",
    "growth-insight",
    "growth-visualization",
    "growth-review",
    "growth-report",
)

TRUST_LEVELS = ("self_asserted", "host_observed", "host_signed")
CAPTURE_METHODS = ("root_cli", "codex_tool_result", "migration")


def is_supported_contract_version(value: object) -> bool:
    return isinstance(value, str) and value in SUPPORTED_CONTRACT_VERSIONS
