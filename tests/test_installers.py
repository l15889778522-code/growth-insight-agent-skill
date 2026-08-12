from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT

import install_skill as installer


INSTALLER = ROOT / "scripts" / "install_skill.py"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


def run_installer(*arguments: object, check: bool = True) -> tuple[subprocess.CompletedProcess[str], dict[str, object] | None]:
    command = [sys.executable, str(INSTALLER), *(str(item) for item in arguments), "--json"]
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if check:
        assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.lstrip("\ufeff")) if result.stdout.strip() else None
    return result, payload


def copy_runtime_source(destination: Path) -> Path:
    spec = installer.collect_skill_package(ROOT)
    for record in spec.files:
        target = destination.joinpath(*Path(record.path).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(record.source, target)
    return destination


def test_skill_manifest_contains_only_runtime_files_and_repeat_is_noop(tmp_path: Path) -> None:
    target = tmp_path / "consumer" / "skill"
    _, first = run_installer("install", "--destination", target)
    assert first is not None
    assert first["status"] == "installed"

    manifest_path = target / installer.SKILL_MANIFEST
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    installed_paths = {item["path"] for item in manifest["files"]}
    assert manifest["schema_version"] == "1.2"
    assert manifest["integrity_scope"] == "manifest-managed-files"
    assert manifest["user_file_policy"] == "preserve-unmanaged"
    assert manifest["package_sha256"] == first["package_sha256"]
    assert "VERSION" in installed_paths
    assert "SKILL.md" in installed_paths
    assert "constraints.txt" in installed_paths
    assert "scripts/runctl.py" in installed_paths
    assert "scripts/evidence.py" in installed_paths
    assert "schemas/run-state.schema.json" in installed_paths
    assert "references/installation.md" in installed_paths
    assert "README.md" not in installed_paths
    assert "requirements-dev.txt" not in installed_paths
    assert "scripts/run_evals.py" not in installed_paths
    assert not any(path.startswith("tests/") for path in installed_paths)
    assert not any(path.startswith("skill-test-harness/") for path in installed_paths)
    assert not (target / "README.md").exists()
    assert not (target / "tests").exists()

    _, repeated = run_installer("install", "--destination", target, "--force")
    assert repeated is not None
    assert repeated["status"] == "unchanged"
    assert repeated["changed"] is False
    assert manifest_path.read_bytes() == manifest_bytes

    _, verified = run_installer("verify", "--destination", target)
    assert verified is not None
    assert verified["status"] == "verified"
    assert verified["ok"] is True

    manifest["files"][0]["size"] += 1
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    failed_verify, failed_payload = run_installer("verify", "--destination", target, check=False)
    assert failed_verify.returncode == 2
    assert failed_payload is not None
    assert failed_payload["status"] == "failed"


def test_dry_run_does_not_create_destination_or_staging(tmp_path: Path) -> None:
    target = tmp_path / "dry-run" / "skill"
    _, result = run_installer("install", "--destination", target, "--dry-run")
    assert result is not None
    assert result["status"] == "planned"
    assert result["dry_run"] is True
    assert not target.exists()
    assert not list(tmp_path.glob(".mada-*"))


def test_deep_unicode_path_uses_short_staging(tmp_path: Path) -> None:
    parent = tmp_path
    index = 0
    while len(str(parent / installer.SKILL_NAME)) < 320:
        parent /= f"数据分析安装层级-{index}-" + ("x" * 32)
        index += 1
    target = parent / installer.SKILL_NAME

    _, result = run_installer("install", "--destination", target)
    assert result is not None
    installed_target = installer._absolute(target)
    assert (installed_target / "SKILL.md").is_file()
    assert (installed_target / "references" / "installation.md").is_file()
    assert result["staging_path_length"] < len(str(target))
    assert "数据分析安装层级" in str(target)

    _, repeated = run_installer("install", "--destination", target)
    assert repeated is not None
    assert repeated["status"] == "unchanged"


def test_modified_managed_file_requires_force_and_uninstall_preserves_user_files(tmp_path: Path) -> None:
    target = tmp_path / "user-content" / "skill"
    run_installer("install", "--destination", target)
    user_file = target / "references" / "用户笔记.txt"
    user_file.write_text("keep me\n", encoding="utf-8")
    environment_marker = target / ".venv" / "local-environment.txt"
    environment_marker.parent.mkdir()
    environment_marker.write_text("mutable\n", encoding="utf-8")
    runtime_cache = target / "scripts" / "__pycache__" / "runtime.pyc"
    runtime_cache.parent.mkdir()
    runtime_cache.write_bytes(b"stale cache")
    expected_skill = (target / "SKILL.md").read_text(encoding="utf-8")
    (target / "SKILL.md").write_text(expected_skill + "\nlocal edit\n", encoding="utf-8")

    failed, payload = run_installer("install", "--destination", target, check=False)
    assert failed.returncode == 2
    assert payload is None
    assert "--force" in failed.stderr

    _, replaced = run_installer("install", "--destination", target, "--force")
    assert replaced is not None
    assert replaced["status"] == "updated"
    assert user_file.read_text(encoding="utf-8") == "keep me\n"
    assert environment_marker.read_text(encoding="utf-8") == "mutable\n"
    assert not runtime_cache.exists()
    assert (target / "SKILL.md").read_text(encoding="utf-8") == expected_skill

    runtime_cache.parent.mkdir()
    runtime_cache.write_bytes(b"generated cache")

    _, removed = run_installer("uninstall", "--destination", target)
    assert removed is not None
    assert removed["status"] == "uninstalled"
    assert user_file.read_text(encoding="utf-8") == "keep me\n"
    assert not (target / "SKILL.md").exists()
    assert not (target / installer.SKILL_MANIFEST).exists()
    assert not (target / ".venv").exists()
    assert not runtime_cache.exists()


def test_failed_upgrade_restores_old_package_and_user_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "rollback" / "skill"
    installer.install_package(installer.collect_skill_package(ROOT), target)
    old_manifest = (target / installer.SKILL_MANIFEST).read_bytes()
    old_skill = (target / "SKILL.md").read_bytes()
    user_file = target / "local" / "用户数据.json"
    user_file.parent.mkdir()
    user_file.write_text('{"keep": true}\n', encoding="utf-8")

    updated_source = copy_runtime_source(tmp_path / "updated-source")
    with (updated_source / "SKILL.md").open("a", encoding="utf-8") as handle:
        handle.write("\ntransaction rollback test\n")
    updated_spec = installer.collect_skill_package(updated_source)
    real_rename = installer._rename
    failed_once = False
    transaction_target = installer._absolute(target)

    def fail_new_package_commit(source: Path, destination: Path) -> None:
        nonlocal failed_once
        if not failed_once and destination == transaction_target and source.name.startswith(".mada-s-"):
            failed_once = True
            raise OSError("injected commit failure")
        real_rename(source, destination)

    monkeypatch.setattr(installer, "_rename", fail_new_package_commit)
    staging_root = tmp_path / "short-stage"
    with pytest.raises(installer.InstallerError, match="rollback was attempted"):
        installer.install_package(updated_spec, target, staging_root=staging_root)

    assert failed_once is True
    assert (target / installer.SKILL_MANIFEST).read_bytes() == old_manifest
    assert (target / "SKILL.md").read_bytes() == old_skill
    assert user_file.read_text(encoding="utf-8") == '{"keep": true}\n'
    assert not list(staging_root.glob(".mada-*"))


def test_dependency_rebuild_failure_restores_old_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "dependency-rollback" / "skill"
    installer.install_package(installer.collect_skill_package(ROOT), target)
    old_manifest = (target / installer.SKILL_MANIFEST).read_bytes()
    old_environment = target / ".venv" / "old-environment.txt"
    old_environment.parent.mkdir()
    old_environment.write_text("old and usable\n", encoding="utf-8")

    updated_source = copy_runtime_source(tmp_path / "dependency-updated-source")
    with (updated_source / "SKILL.md").open("a", encoding="utf-8") as handle:
        handle.write("\ndependency rollback test\n")

    def fail_environment(destination: Path, _python_executable: str) -> None:
        environment = destination / ".venv"
        environment.mkdir()
        (environment / "partial.txt").write_text("partial\n", encoding="utf-8")
        raise OSError("injected dependency failure")

    monkeypatch.setattr(installer, "_create_runtime_environment", fail_environment)
    with pytest.raises(installer.InstallerError, match="rollback was attempted"):
        installer.install_package(
            installer.collect_skill_package(updated_source),
            target,
            staging_root=tmp_path / "dependency-stage",
            install_dependencies=True,
        )

    assert (target / installer.SKILL_MANIFEST).read_bytes() == old_manifest
    assert old_environment.read_text(encoding="utf-8") == "old and usable\n"
    assert not (target / ".venv" / "partial.txt").exists()


def test_runtime_environment_uses_subprocess_compatible_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = installer._absolute(tmp_path / "runtime-\u4e2d\u6587" / "skill")
    commands: list[list[str]] = []

    monkeypatch.setattr(
        installer,
        "_venv_python",
        lambda venv: venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python"),
    )
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda command, check: commands.append(command),
    )

    installer._create_runtime_environment(destination, sys.executable)

    assert commands[0][-1] == installer._subprocess_path(destination / ".venv")
    assert commands[1][0] == installer._subprocess_path(installer._venv_python(destination / ".venv"))
    assert commands[1][-1] == installer._subprocess_path(destination / "requirements.txt")
    if os.name == "nt":
        assert all(not value.startswith("\\\\?\\") for command in commands for value in command)


def test_custom_agent_uninstall_preserves_other_agent_files(tmp_path: Path) -> None:
    target = tmp_path / "shared-agents"
    _, installed = run_installer("install-agents", "--destination", target)
    assert installed is not None
    assert len(list(target.glob("growth-*.toml"))) == 7
    personal_agent = target / "personal-agent.toml"
    personal_agent.write_text('name = "personal"\n', encoding="utf-8")

    _, removed = run_installer("uninstall-agents", "--destination", target)
    assert removed is not None
    assert personal_agent.is_file()
    assert not list(target.glob("growth-*.toml"))
    assert not (target / installer.AGENTS_MANIFEST).exists()


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is not available")
def test_powershell_project_agent_install_is_compatible(tmp_path: Path) -> None:
    project = tmp_path / "PowerShell-中文-project"
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
    manifest = json.loads((target / installer.AGENTS_MANIFEST).read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "1.2"
    assert manifest["agent_contract_version"] == "1.2"
    assert {item["name"] for item in manifest["agents"]} == set(installer.REQUIRED_AGENT_NAMES)
    for item in manifest["files"]:
        source = ROOT / "assets" / "custom-agents" / item["path"]
        installed = target / item["path"]
        assert source.read_bytes() == installed.read_bytes()

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
    preflight_result = json.loads(preflight.stdout.lstrip("\ufeff"))
    assert preflight_result["static_checks_passed"] is True
    assert preflight_result["manifest_present"] is True
    assert all(item["ok"] for item in preflight_result["agents"])


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is not available")
def test_powershell_skill_wrapper_supports_dry_run(tmp_path: Path) -> None:
    target = tmp_path / "PowerShell-中文-dry-run" / "skill"
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "install_skill.ps1"),
            "-Destination",
            str(target),
            "-DryRun",
            "-Json",
            "-PythonExecutable",
            sys.executable,
        ],
        cwd=ROOT,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.lstrip("\ufeff"))
    assert payload["status"] == "planned"
    assert not target.exists()
