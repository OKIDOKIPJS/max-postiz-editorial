"""Entrypoint checks never invoke Docker or overwrite an existing generic config."""

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh")
GIT_BASH = Path(r"C:\Program Files\Git\bin\bash.exe")
BASH = str(GIT_BASH) if GIT_BASH.is_file() else (shutil.which("bash") if os.name != "nt" else None)


def fixture_copy(tmp_path, relative):
    copy, source = tmp_path / "copy", tmp_path / "source"
    source.mkdir()
    target = copy / relative
    target.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / relative, target)
    return copy, source, target


def run(command):
    # No inherited service credentials or package-manager configuration.
    names = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "COMSPEC"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in names}
    return subprocess.run(command, capture_output=True, text=True, timeout=30, env=environment)


@pytest.mark.skipif(not POWERSHELL, reason="PowerShell is unavailable")
def test_windows_installer_check_is_read_only_and_refuses_generic_config(tmp_path):
    copy, source, script = fixture_copy(tmp_path, "scripts/install-four-platform.ps1")
    command = [POWERSHELL, "-NoProfile", "-File", str(script), "-SourceRoot", str(source), "-Check"]
    initial = sorted(path.relative_to(copy).as_posix() for path in copy.rglob("*"))
    result = run(command)
    assert result.returncode == 0 and "FOUR_PLATFORM_INSTALL_CHECK_OK" in result.stdout
    assert initial == sorted(path.relative_to(copy).as_posix() for path in copy.rglob("*"))
    (copy / "config").mkdir()
    app = copy / "config/app.json"
    app.write_text('{"legacy":true}')
    blocked = run(command)
    assert blocked.returncode != 0 and "ISOLATED_EXISTING_CONFIGURATION_FORBIDDEN" in blocked.stderr
    assert app.read_text() == '{"legacy":true}' and not (copy / ".venv").exists()


@pytest.mark.skipif(not POWERSHELL, reason="PowerShell is unavailable")
def test_legacy_windows_postiz_refuses_marker_before_docker_discovery(tmp_path):
    copy, _, script = fixture_copy(tmp_path, "scripts/postiz.ps1")
    marker = copy / "data/four-platform/isolation.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}")
    result = run([POWERSHELL, "-NoProfile", "-File", str(script), "-Action", "start"])
    assert result.returncode != 0 and "ISOLATED_LEGACY_POSTIZ_FORBIDDEN" in result.stderr
    assert "Docker CLI is not installed" not in result.stderr
    assert not (copy / "deployment").exists()


@pytest.mark.skipif(not BASH, reason="Native Bash is unavailable; WSL is not launched")
def test_shell_installers_parse_and_legacy_copy_refuses_before_absolute_root(tmp_path):
    for relative in ("scripts/install-four-platform.sh", "deployment/linux/postiz.sh"):
        assert run([BASH, "--noprofile", "--norc", "-n", str(ROOT / relative)]).returncode == 0
    copy, _, script = fixture_copy(tmp_path, "deployment/linux/postiz.sh")
    marker = copy / "data/four-platform/isolation.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}")
    result = run([BASH, "--noprofile", "--norc", str(script), "up", "-d"])
    assert result.returncode == 2 and "ISOLATED_LEGACY_POSTIZ_FORBIDDEN" in result.stderr


@pytest.mark.skipif(not BASH, reason="Native Bash is unavailable; WSL is not launched")
def test_shell_installer_read_only_check(tmp_path):
    copy, source, script = fixture_copy(tmp_path, "scripts/install-four-platform.sh")
    result = run([BASH, "--noprofile", "--norc", str(script), "--source-root", str(source),
                  "--python", sys.executable, "--check"])
    assert result.returncode == 0 and "FOUR_PLATFORM_INSTALL_CHECK_OK" in result.stdout
    assert not (copy / ".venv").exists() and not (copy / "config").exists()
