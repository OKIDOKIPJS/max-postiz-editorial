"""Границы конфигурационных путей и приватных файлов проверяются на временных данных."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from max_postiz_agent import panel_files, panel_settings
from max_postiz_agent.config import AgentError, Settings, inside
from max_postiz_agent.panel_settings import save_configuration, snapshot, validate_configuration


def configuration(project):
    return snapshot(SimpleNamespace(settings=Settings.load(project)))["configuration"]


@pytest.mark.parametrize("field,value", [
    ("topic_path", "config/topics/../../docs/changed.md"),
    ("sources_path", "config/sources/../../config/changed.json"),
    ("topic_path", "config/topics/nested/../../../TOPIC.md"),
    ("sources_path", "config/sources/..\\..\\schemas/sources.schema.json"),
])
def test_settings_paths_cannot_escape_their_allowed_namespace(project, field, value):
    document = configuration(project)
    document["app"][field] = value
    before = (project / "TOPIC.md").read_bytes()
    with pytest.raises(AgentError, match="SETTINGS_PATH_INVALID"):
        validate_configuration(project, document)
    assert (project / "TOPIC.md").read_bytes() == before
    assert not (project / "docs/changed.md").exists()
    assert not (project / "config/changed.json").exists()


@pytest.mark.parametrize("source_path,escape_topic,error", [
    ("config/sources.json", False, "CONFIG_SCHEMA_INVALID"),
    ("config/sources/../../schemas/sources.schema.json", False, "SETTINGS_PATH_INVALID"),
    ("config/sources/../../schemas/sources.schema.json", True, "SETTINGS_PATH_INVALID"),
])
def test_source_payload_cannot_poison_the_schema_used_for_its_own_validation(project, monkeypatch, source_path, escape_topic, error):
    document = configuration(project)
    document["sources"]["sources"][0]["trust"] = "synthetic-invalid-trust"
    document["sources"].update(type="object", additionalProperties=True)
    # Такой объект одновременно является невалидными source data и разрешающей JSON Schema.
    Draft202012Validator.check_schema(document["sources"])
    document["app"]["sources_path"] = source_path
    if escape_topic:
        document["app"]["topic_path"] = "config/topics/../../docs/synthetic-chain.md"
    protected = [project / name for name in ("schemas/sources.schema.json", "config/app.json", "config/sources.json")]
    before = [path.read_bytes() for path in protected]
    written_names = []
    original = panel_settings.atomic_json
    def atomic_json(root, name, value):
        written_names.append(name)
        return original(root, name, value)
    monkeypatch.setattr(panel_settings, "atomic_json", atomic_json)
    with pytest.raises(AgentError, match=error):
        validate_configuration(project, document)
    if error == "SETTINGS_PATH_INVALID":
        assert written_names == []
    else:
        assert "config/sources.json" in written_names
    assert [path.read_bytes() for path in protected] == before
    assert not (project / "docs/synthetic-chain.md").exists()


@pytest.mark.parametrize("value", [
    "config/../schemas/replacement.json", "config/./workflow.json",
    "config/app.json", "config/image_channels.json", "config/sources.json",
    "config/image_sources.json", "config/holidays.json",
])
def test_workflow_target_cannot_escape_or_replace_validated_configuration(project, value):
    document = configuration(project)
    document["app"]["video"]["workflow_path"] = value
    document["workflow"] = {"1": {"class_type": "Fixture", "inputs": {}}}
    with pytest.raises(AgentError, match="WORKFLOW_PATH_INVALID"):
        validate_configuration(project, document)


def test_workflow_collision_is_rejected_before_live_configuration_changes(service, project, monkeypatch):
    monkeypatch.setenv("APP_AUTH_SECRET", "security-path-test-owner")
    document = snapshot(service)["configuration"]
    document["app"]["video"]["workflow_path"] = "config/image_channels.json"
    document["workflow"] = {"1": {"class_type": "Fixture", "inputs": {}}}
    before = (project / "config/app.json").read_bytes()
    with pytest.raises(AgentError, match="WORKFLOW_PATH_INVALID"):
        save_configuration(service, document)
    assert (project / "config/app.json").read_bytes() == before
    assert not (project / "config/image_channels.json").exists()


@pytest.mark.parametrize("field,value,victim", [
    ("topic_path", "config/topics/../../docs/manual.md", "docs/manual.md"),
    ("sources_path", "config/sources/../../config/unrelated.json", "config/unrelated.json"),
])
def test_scoped_path_escape_cannot_overwrite_an_existing_project_file(service, project, monkeypatch, field, value, victim):
    monkeypatch.setenv("APP_AUTH_SECRET", "security-path-test-owner")
    target = project / victim
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("synthetic-original-content", encoding="utf-8")
    document = snapshot(service)["configuration"]
    document["app"][field] = value
    before = (project / "config/app.json").read_bytes()
    with pytest.raises(AgentError, match="SETTINGS_PATH_INVALID"):
        save_configuration(service, document)
    assert target.read_text(encoding="utf-8") == "synthetic-original-content"
    assert (project / "config/app.json").read_bytes() == before


@pytest.mark.parametrize("value", ["data/private-imports/access.json", "config/opencode.private.json"])
def test_workflow_none_does_not_allow_a_private_path_to_be_read_on_next_snapshot(project, value):
    document = configuration(project)
    document["app"]["video"]["workflow_path"] = value
    document["workflow"] = None
    with pytest.raises(AgentError, match="WORKFLOW_PATH_INVALID"):
        validate_configuration(project, document)


def configured_private_profile(app, directory):
    if directory == "browser":
        app["max"]["browser"]["profile_dir"] = "config/browser-state"
        return "config/browser-state"
    app["opencode"]["isolated_state_dir"] = "config/opencode-state"
    return "config/opencode-state"


@pytest.mark.parametrize("directory", ["browser", "opencode"])
def test_configured_private_profile_cannot_be_workflow_with_none_document(project, directory):
    document = configuration(project)
    private = configured_private_profile(document["app"], directory)
    document["app"]["video"]["workflow_path"] = private + "/session.json"
    document["workflow"] = None
    with pytest.raises(AgentError, match="WORKFLOW_PATH_INVALID"):
        validate_configuration(project, document)


@pytest.mark.parametrize("directory", ["browser", "opencode"])
def test_configured_private_profile_snapshot_is_rejected_before_file_read(project, directory, monkeypatch):
    settings = Settings.load(project)
    private = configured_private_profile(settings.app, directory)
    target = project / private / "session.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"metadata":"synthetic-private-profile"}', encoding="utf-8")
    settings.app["video"]["workflow_path"] = private + "/session.json"
    original = Path.read_text
    def read_text(path, *args, **kwargs):
        if path == target:
            pytest.fail("Private profile file must be rejected before reading")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read_text)
    with pytest.raises(AgentError, match="WORKFLOW_PATH_INVALID"):
        snapshot(SimpleNamespace(settings=settings))


def test_configured_private_roots_keep_metadata_and_sibling_contracts(project):
    settings = Settings.load(project)
    browser = configured_private_profile(settings.app, "browser")
    opencode = configured_private_profile(settings.app, "opencode")
    service = SimpleNamespace(settings=settings)
    for relative in (browser, opencode):
        target = project / relative / "session.json"
        target.parent.mkdir(parents=True)
        target.write_text('{"metadata":"synthetic-private-profile"}', encoding="utf-8")
    assert panel_files.selected(service, opencode + "/session.json", purpose="private")["state"] == "selected"
    with pytest.raises(AgentError, match="FILE_DIRECTORY_PRIVATE"):
        panel_files.selected(service, browser + "/session.json", purpose="private")
    sibling = project / "config/browser-state-sibling/workflow.json"
    sibling.parent.mkdir()
    sibling.write_text('{"1":{"class_type":"Fixture","inputs":{}}}', encoding="utf-8")
    settings.app["video"]["workflow_path"] = sibling.relative_to(project).as_posix()
    assert snapshot(service)["configuration"]["workflow"]["1"]["class_type"] == "Fixture"


@pytest.mark.parametrize("name", ["opencode.private.json", "OPENCODE.PRIVATE.JSON"])
def test_known_private_configuration_is_not_a_selectable_workflow(project, name):
    service = SimpleNamespace(settings=Settings.load(project))
    path = project / "config" / name
    path.write_text('{"metadata":"synthetic-private-value"}', encoding="utf-8")
    with pytest.raises(AgentError, match="FILE_PATH_FORBIDDEN"):
        panel_files.selected(service, "config/" + name, purpose="workflow")


@pytest.mark.parametrize("directory", ["data/max-browser", "data/opencode", "data/private-imports"])
def test_direct_workflow_selection_cannot_read_private_directories(project, directory):
    service = SimpleNamespace(settings=Settings.load(project))
    path = project / directory / "synthetic-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"metadata":"synthetic-private-value"}', encoding="utf-8")
    with pytest.raises(AgentError, match="FILE_DIRECTORY_PRIVATE"):
        panel_files.selected(service, path.relative_to(project).as_posix(), purpose="workflow")


def test_default_browser_profile_is_private_when_profile_dir_is_implicit(project):
    service = SimpleNamespace(settings=Settings.load(project))
    service.settings.app["max"]["browser"].pop("profile_dir", None)
    path = project / "data/max-browser/state.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"metadata":"synthetic-private-value"}', encoding="utf-8")
    with pytest.raises(AgentError, match="FILE_DIRECTORY_PRIVATE"):
        panel_files.browse(service, "data/max-browser", purpose="workflow")
    with pytest.raises(AgentError, match="FILE_DIRECTORY_PRIVATE"):
        panel_files.selected(service, "data/max-browser/state.json", purpose="workflow")


@pytest.mark.parametrize("directory,section,key", [
    ("data/max-browser", "browser", "profile_dir"),
    ("data/opencode", "opencode", "isolated_state_dir"),
])
def test_known_default_private_roots_remain_private_after_relocation(project, directory, section, key):
    service = SimpleNamespace(settings=Settings.load(project))
    config = (service.settings.app["max"]["browser"] if section == "browser"
              else service.settings.app["opencode"])
    config[key] = "config/relocated-" + section
    path = project / directory / "synthetic-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"metadata":"synthetic-private-value"}', encoding="utf-8")
    with pytest.raises(AgentError, match="FILE_DIRECTORY_PRIVATE"):
        panel_files.selected(service, path.relative_to(project).as_posix(), purpose="workflow")
    if section == "opencode":
        result = panel_files.selected(service, path.relative_to(project).as_posix(), purpose="private")
        assert result["state"] == "selected" and "document" not in result


def test_allowed_configuration_namespaces_and_private_metadata_remain_usable(project):
    document = configuration(project)
    document["app"].update(topic_path="config/topics/science.md", sources_path="config/sources/science.json")
    document["app"]["video"]["workflow_path"] = "config/comfy/science.json"
    document["workflow"] = {"1": {"class_type": "Fixture", "inputs": {}}}
    assert validate_configuration(project, document)
    service = SimpleNamespace(settings=Settings.load(project))
    path = project / "data/private-imports/access.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"metadata":"synthetic-private-value"}', encoding="utf-8")
    result = panel_files.selected(service, path.relative_to(project).as_posix(), purpose="private")
    assert result["state"] == "selected" and "document" not in result
    sibling = project / "data/max-browser-sibling/public.json"
    sibling.parent.mkdir(parents=True)
    sibling.write_text('{"1":{"class_type":"Fixture","inputs":{}}}', encoding="utf-8")
    assert panel_files.selected(service, sibling.relative_to(project).as_posix(), purpose="workflow")["document"]


def symlink(target: Path, link: Path):
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError:
        pytest.skip("Symlink creation is unavailable on this host")


def test_configuration_symlink_cannot_redirect_a_scoped_write(project):
    document = configuration(project)
    directory = project / "config/topics"
    directory.mkdir()
    symlink(project / "TOPIC.md", directory / "alias.md")
    document["app"]["topic_path"] = "config/topics/alias.md"
    with pytest.raises(AgentError, match="SETTINGS_PATH_INVALID"):
        validate_configuration(project, document)


def test_private_profile_symlink_cannot_bypass_direct_selection(project):
    service = SimpleNamespace(settings=Settings.load(project))
    target = project / "data/max-browser/state.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"metadata":"synthetic-private-value"}', encoding="utf-8")
    symlink(target, project / "config/visible.json")
    with pytest.raises(AgentError, match="FILE_DIRECTORY_PRIVATE"):
        panel_files.selected(service, "config/visible.json", purpose="workflow")


def test_private_imports_resolved_into_config_cannot_be_read_as_workflow(project, monkeypatch):
    settings = Settings.load(project)
    target = project / "config/imported-private/session.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"metadata":"synthetic-private-import"}', encoding="utf-8")
    (project / "data").mkdir(exist_ok=True)
    symlink(target.parent, project / "data/private-imports")
    settings.app["video"]["workflow_path"] = target.relative_to(project).as_posix()
    original = Path.read_text
    def read_text(path, *args, **kwargs):
        if path == target:
            pytest.fail("Resolved private-import file must be rejected before reading")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read_text)
    with pytest.raises(AgentError, match="WORKFLOW_PATH_INVALID"):
        snapshot(SimpleNamespace(settings=settings))


@pytest.mark.parametrize("value", ["../escape.txt", "config/../../escape.txt"])
def test_existing_project_containment_blocks_parent_traversal(project, value):
    with pytest.raises(AgentError, match="PATH_OUTSIDE_PROJECT"):
        inside(project, value)


def test_existing_project_containment_blocks_external_absolute_and_symlink_paths(project, tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    with pytest.raises(AgentError, match="PATH_OUTSIDE_PROJECT"):
        inside(project, str(outside / "fixture.json"))
    symlink(outside, project / "outside-alias")
    with pytest.raises(AgentError, match="PATH_OUTSIDE_PROJECT"):
        inside(project, "outside-alias/fixture.json")


@pytest.mark.parametrize("channel_id", ["../escape", "nested/name", "..\\escape", "C:escape", "", "."])
def test_channel_names_cannot_become_filesystem_paths(project, channel_id):
    from max_postiz_agent.channel_profiles import channel_path
    from max_postiz_agent.image_pipeline import latest
    from max_postiz_agent.image_schedule import read, write

    for operation in (
        lambda: channel_path(channel_id, "latest.json"),
        lambda: latest(project, channel_id),
        lambda: read(project, "control", {}, channel_id),
        lambda: write(project, "control", {}, channel_id),
    ):
        with pytest.raises(AgentError, match="IMAGE_CHANNEL_ID_INVALID"):
            operation()


@pytest.mark.parametrize("method,suffix", [("inspect_image", ".png"), ("inspect_video", ".mp4")])
def test_media_paths_are_contained_before_any_decoder_or_process(project, method, suffix, monkeypatch):
    from PIL import Image
    from max_postiz_agent import media

    def forbidden(*args, **kwargs):
        pytest.fail("An outside-project path must not reach a decoder or subprocess")
    monkeypatch.setattr(Image, "open", forbidden)
    monkeypatch.setattr(media.subprocess, "run", forbidden)
    for value in ("../synthetic-outside" + suffix, str(project.parent / (project.name + "-outside" + suffix))):
        with pytest.raises(AgentError, match="PATH_OUTSIDE_PROJECT"):
            getattr(media, method)(project, value)
