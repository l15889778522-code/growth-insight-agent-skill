from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import ROOT


POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is not available")
def test_project_install_and_preflight_are_reproducible(tmp_path: Path) -> None:
    project = tmp_path / "consumer-project"
    install = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "install_custom_agents.ps1"),
            "-Scope",
            "Project",
            "-ProjectPath",
            str(project),
        ],
        cwd=ROOT,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert install.returncode == 0, install.stderr
    target = project / ".codex" / "agents"
    assert len(list(target.glob("growth-*.toml"))) == 7
    assert (target / "multi-agent-data-analysis-agents.manifest.json").is_file()

    preflight = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "codex_agents_preflight.ps1"),
            "-Scope",
            "Project",
            "-ProjectPath",
            str(project),
            "-Json",
        ],
        cwd=ROOT,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert preflight.returncode == 0, preflight.stderr
    result = json.loads(preflight.stdout.lstrip("\ufeff"))
    assert result["static_checks_passed"] is True
    assert result["manifest_present"] is True
    assert result["runtime_spawn_required"] is True
    assert all(item["ok"] for item in result["agents"])


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is not available")
def test_project_skill_install_uses_official_repo_skill_location(tmp_path: Path) -> None:
    project = tmp_path / "consumer-project"
    install = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "install_skill.ps1"),
            "-Scope",
            "Project",
            "-ProjectPath",
            str(project),
        ],
        cwd=ROOT,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert install.returncode == 0, install.stderr
    target = project / ".agents" / "skills" / "multi-agent-data-analysis-skill"
    assert (target / "SKILL.md").is_file()
    assert (target / "scripts" / "runctl.py").is_file()
    assert (target / "schemas" / "run-state.schema.json").is_file()
    assert (target / "tests" / "evals" / "cases.json").is_file()
    assert (target / "multi-agent-data-analysis-skill.manifest.json").is_file()
    installed_instructions = (target / "SKILL.md").read_text(encoding="utf-8")
    assert "<skill-root>/.venv/Scripts/python.exe" in installed_instructions
    assert "Never resolve this Skill's" in installed_instructions
    assert not list((target / "tests").glob("test_*.py"))
    assert not (target / ".venv").exists()

    (target / "SKILL.md").write_text(installed_instructions + "\nmodified\n", encoding="utf-8")
    reinstall = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "install_skill.ps1"),
            "-Scope",
            "Project",
            "-ProjectPath",
            str(project),
        ],
        cwd=ROOT,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert reinstall.returncode == 1
    assert "-Force" in reinstall.stderr
