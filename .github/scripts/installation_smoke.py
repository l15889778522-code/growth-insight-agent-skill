#!/usr/bin/env python3
"""Exercise a clean install, verification, preflight, and uninstall lifecycle."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    result = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}: {command!r}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def parse_json_output(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return json.loads(result.stdout.lstrip("\ufeff"))


def venv_python(skill_destination: Path) -> Path:
    candidates = (
        skill_destination / ".venv" / "Scripts" / "python.exe",
        skill_destination / ".venv" / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("Installed Skill virtual environment has no Python executable.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    args = parser.parse_args()

    source_root = args.source_root.resolve()
    work_root = args.work_root.resolve() / "fresh-install-\u4e2d\u6587"
    if work_root.exists():
        raise RuntimeError(f"Smoke work directory must be new: {work_root}")
    work_root.mkdir(parents=True)

    installer = source_root / "scripts" / "install_skill.py"
    skill_destination = work_root / "skill"
    agents_destination = work_root / "agents"
    base = [sys.executable, str(installer)]

    run(
        base
        + [
            "install",
            "--source-root",
            str(source_root),
            "--destination",
            str(skill_destination),
            "--install-dependencies",
            "--force",
        ],
        cwd=source_root,
    )
    run(
        base
        + [
            "verify",
            "--source-root",
            str(source_root),
            "--destination",
            str(skill_destination),
        ],
        cwd=source_root,
    )
    run(
        base
        + [
            "install-agents",
            "--source-root",
            str(source_root),
            "--destination",
            str(agents_destination),
            "--force",
        ],
        cwd=source_root,
    )
    run(
        base
        + [
            "verify-agents",
            "--source-root",
            str(source_root),
            "--destination",
            str(agents_destination),
        ],
        cwd=source_root,
    )

    dependency_result = parse_json_output(
        run(
            [
                str(venv_python(skill_destination)),
                str(skill_destination / "scripts" / "check_runtime_dependencies.py"),
            ],
            cwd=skill_destination,
        )
    )
    if dependency_result.get("ok") is not True:
        raise RuntimeError(f"Installed runtime dependency check failed: {dependency_result}")

    powershell = (
        shutil.which("pwsh")
        or shutil.which("powershell")
        or shutil.which("powershell.exe")
    )
    if not powershell:
        raise RuntimeError("PowerShell is required for the installed Agent preflight.")
    preflight_result = parse_json_output(
        run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(source_root / "scripts" / "codex_agents_preflight.ps1"),
                "-Destination",
                str(agents_destination),
                "-Json",
            ],
            cwd=source_root,
        )
    )
    if preflight_result.get("static_checks_passed") is not True:
        raise RuntimeError(f"Installed Agent preflight failed: {preflight_result}")

    skill_note = skill_destination / "user-note.txt"
    agent_note = agents_destination / "user-note.txt"
    skill_note.write_text("preserve me\n", encoding="utf-8")
    agent_note.write_text("preserve me\n", encoding="utf-8")

    run(
        base
        + [
            "uninstall-agents",
            "--destination",
            str(agents_destination),
            "--force",
        ],
        cwd=source_root,
    )
    run(
        base
        + [
            "uninstall",
            "--destination",
            str(skill_destination),
            "--force",
        ],
        cwd=source_root,
    )

    if sorted(path.name for path in skill_destination.iterdir()) != [skill_note.name]:
        raise RuntimeError("Skill uninstall left managed files or removed the user file.")
    if sorted(path.name for path in agents_destination.iterdir()) != [agent_note.name]:
        raise RuntimeError("Agent uninstall left managed files or removed the user file.")

    print(
        json.dumps(
            {
                "ok": True,
                "source_root": str(source_root),
                "unicode_destination": True,
                "runtime_dependencies": "verified",
                "agent_static_preflight": "verified",
                "uninstall_preserved_user_files": True,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
