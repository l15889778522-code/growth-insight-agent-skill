#!/usr/bin/env python3
"""Check the isolated runtime dependencies without installing anything."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import sys
from pathlib import Path

from contracts import CURRENT_CONTRACT_VERSION


PACKAGES = {
    "jsonschema": "jsonschema",
    "sqlglot": "sqlglot",
    "pymysql": "PyMySQL",
    "matplotlib": "matplotlib",
    "plotly": "plotly",
}


def check(include_dev: bool = False) -> dict[str, object]:
    packages = dict(PACKAGES)
    if include_dev:
        packages.update({"pytest": "pytest", "coverage": "coverage"})
    records = []
    for import_name, distribution_name in packages.items():
        try:
            version = importlib.metadata.version(distribution_name)
            __import__(import_name)
            records.append({"package": distribution_name, "import": import_name, "version": version, "ok": True, "error": None})
        except Exception as exc:
            records.append({"package": distribution_name, "import": import_name, "version": None, "ok": False, "error": str(exc)})
    python_ok = (3, 11) <= sys.version_info[:2] <= (3, 13)
    return {
        "schema_version": CURRENT_CONTRACT_VERSION,
        "python": platform.python_version(),
        "python_executable": str(Path(sys.executable).resolve()),
        "python_ok": python_ok,
        "packages": records,
        "ok": python_ok and all(record["ok"] for record in records),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=f"Check v{CURRENT_CONTRACT_VERSION} runtime dependencies.")
    parser.add_argument("--include-dev", action="store_true")
    args = parser.parse_args()
    result = check(args.include_dev)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
