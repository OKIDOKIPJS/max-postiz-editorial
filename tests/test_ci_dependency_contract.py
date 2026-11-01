"""Deployment pins must participate in the same dependency safety gate as CI."""

import importlib.util
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location(
    "ci_dependency_contract", Path(__file__).resolve().parents[1] / "scripts/check_ci_dependencies.py")
dependency_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dependency_check)


@pytest.mark.parametrize("deployment_pin", ["PyYAML==6.0.3", None, "PyYAML==6.0.2"])
def test_ci_manifest_includes_the_exact_deployment_dependency(tmp_path, deployment_pin):
    files = {
        "requirements.lock": "runtime==1.0\n",
        "requirements-dev.lock": "development==2.0\n",
        "requirements-browser.lock": "browser==3.0\n",
        "requirements-deployment.lock": "PyYAML==6.0.3\n",
    }
    for name, contents in files.items():
        (tmp_path / name).write_text(contents, encoding="utf-8")
    manifest = "runtime==1.0\ndevelopment==2.0\nbrowser==3.0\n"
    if deployment_pin is not None:
        manifest += deployment_pin + "\n"
    (tmp_path / "requirements-ci.txt").write_text(manifest, encoding="utf-8")
    if deployment_pin == "PyYAML==6.0.3":
        assert dependency_check.check(tmp_path) == 4
    else:
        with pytest.raises(dependency_check.DependencyCheckError, match="missing pins|version mismatch"):
            dependency_check.check(tmp_path)
