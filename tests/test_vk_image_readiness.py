"""VK-only local doctor proof; no browser, DB, network or other platform cases."""
import json

import pytest

from max_postiz_agent.config import AgentError
from max_postiz_agent.route_readiness import _target


class Settings:
    def channel(self, ident):
        assert ident == "test"
        return {"enabled": True, "publication": {"mode": "review"}}


@pytest.fixture
def local_image(tmp_path, monkeypatch):
    from max_postiz_agent import route_readiness

    (tmp_path / "data/vk-browser").mkdir(parents=True)
    monkeypatch.setattr(route_readiness, "_LINUX", False)
    monkeypatch.setattr(route_readiness.importlib.util, "find_spec", lambda name: object())
    value = {"id": "vk_test", "profile_id": "test", "platform": "vk", "route": "vk_browser",
        "destination": "123456", "enabled": True, "options": {
            "owner": "123456", "dom_contract_profile": "personal-image-v1",
            "dom_contract_verified": True, "media_contract_verified": True}}
    return tmp_path, value


def test_vk_image_media_flag_without_evidence_is_not_local_readiness(local_image):
    root, value = local_image
    row = _target(root, Settings(), value, {})
    assert row["media_contract_verified"] is False
    assert row["local_prerequisites_passed"] is False
    assert "VK_BROWSER_MEDIA_EVIDENCE_UNVERIFIED" in row["blockers"]
    assert row["access"] == row["delivery"] == "not_checked"
    assert "123456" not in json.dumps(row) and str(root) not in json.dumps(row)


@pytest.mark.parametrize("error", ["VK_BROWSER_MEDIA_EVIDENCE_INVALID", "VK_BROWSER_MEDIA_POLICY_REQUIRED"])
def test_vk_image_evidence_failure_is_redacted(local_image, monkeypatch, error):
    from max_postiz_agent import vk_browser, vk_media

    root, value = local_image
    monkeypatch.setattr(vk_browser, "dom_contract", lambda options, **kwargs: {"fixture": "contract"})
    def failed(*args):
        raise AgentError(error)
    monkeypatch.setattr(vk_media, "require_contract", failed)
    row = _target(root, Settings(), value, {})
    assert row["media_contract_verified"] is False
    assert row["dom_contract_complete"] is True
    assert "VK_BROWSER_MEDIA_EVIDENCE_UNVERIFIED" in row["blockers"]
    assert error not in json.dumps(row)


def test_vk_image_contract_success_reports_only_local_integrity(local_image, monkeypatch):
    from max_postiz_agent import vk_browser, vk_media

    root, value = local_image
    selectors = {"fixture": "observed"}
    monkeypatch.setattr(vk_browser, "dom_contract", lambda options, **kwargs: selectors)
    seen = []
    def valid(actual_root, options, dom, owner):
        seen.append((actual_root, options, dom, owner))
        return {"contract": "fixture"}
    monkeypatch.setattr(vk_media, "require_contract", valid)
    row = _target(root, Settings(), value, {})
    assert seen == [(root, value["options"], selectors, "123456")]
    assert row["media_contract_verified"] is True and row["local_prerequisites_passed"] is True
    assert row["access"] == row["delivery"] == row["approval"] == "not_checked"


def test_vk_image_contract_cannot_address_another_wall(local_image):
    root, value = local_image
    value["destination"] = "-123456"
    with pytest.raises(AgentError, match="VK_BROWSER_PERSONAL_WALL_REQUIRED"):
        _target(root, Settings(), value, {})
