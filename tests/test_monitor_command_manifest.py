"""The same side-effect-free command grammar drives help, reader and gateway."""

import pytest

from max_postiz_agent.monitor_command_manifest import (
    COMMAND_SPECS, OWNER_ACTIONS, classify_command, parse_command_text, source_url_is_safe,
)


@pytest.mark.parametrize("text,action", [
    ("команды", "help"), ("/ПОМОЩЬ", "help"), ("статус", "status"), ("резерв", "reserve"),
    ("ошибки", "errors"), ("источники", "sources"), ("пополни резерв до 6", "refill"),
    ("задачи", "tasks"), ("задача " + "a" * 32, "task"), ("отмени задачу " + "b" * 32, "cancel"),
    ("продолжи подготовку", "resume_refill"), ("сними общую паузу", "resume_project"),
    ("диагностика", "diagnostics"), ("пауза подготовки", "pause"), ("сверь dispatch-1", "reconcile"),
    ("сними блокировку max_test", "unblock"), ("добавь источник https://example.org/", "source_propose"),
    ("измени фильтр авторы: Автор", "source_settings"), ("подтверждаю " + "c" * 32, "source_settings"),
])
def test_every_supported_action_has_shared_classification(text, action):
    assert classify_command(text) == action and parse_command_text(text)["action"] == action


def test_manifest_has_exact_unique_actions_and_no_executors():
    assert len(COMMAND_SPECS) == len(OWNER_ACTIONS) == 17
    assert {spec["action"] for spec in COMMAND_SPECS} == OWNER_ACTIONS
    assert all(set(spec) == {"action", "syntax", "description", "aliases"} for spec in COMMAND_SPECS)
    assert all(not callable(value) for spec in COMMAND_SPECS for value in spec.values())


@pytest.mark.parametrize("text", [None, "", "статус" + " " * 1201, "shell rm file", "пополни резерв до 0",
                                "пополни резерв до 13", "задача ../id", "Копия проекта\nкоманды\nстатус"])
def test_unrecognized_text_never_becomes_an_action(text):
    assert parse_command_text(text) is None and classify_command(text) is None


@pytest.mark.parametrize("url", ["http://example.org/", "https://127.0.0.1/", "https://localhost/",
                               "https://user:pass@example.org/", "https://@example.org/", "https://example.org/?",
                               "https://example.org/#", "https://example.org/?key=value", "https://example.org/\\path"])
def test_source_url_validation_is_local_and_rejects_forbidden_forms(url):
    assert source_url_is_safe(url) is False


def test_valid_source_url_is_only_a_candidate_payload():
    url = "https://example.org/section/"
    assert source_url_is_safe(url) is True
    assert parse_command_text("добавь источник " + url) == {"action": "source_propose", "url": url}


@pytest.mark.parametrize("text,field", [("очисти фильтр ключевые слова", "keywords"),
                                       ("/ОЧИСТИ ФИЛЬТР АВТОРЫ", "authors")])
def test_explicit_clear_is_same_source_settings_action_and_empty_proposal(text, field):
    assert parse_command_text(text) == {"action": "source_settings", "field": field, "values": []}
    assert classify_command(text) == "source_settings"
    spec = next(spec for spec in COMMAND_SPECS if spec["action"] == "source_settings")
    assert "очисти фильтр ключевые слова" in spec["aliases"]
    assert "очисти фильтр авторы" in spec["aliases"]
    assert len(OWNER_ACTIONS) == 17


@pytest.mark.parametrize("text", ["очисти фильтр", "очисти фильтр всё", "очисти фильтр авторы: имя",
                                "измени фильтр ключевые слова:", "измени фильтр авторы:"])
def test_clearing_requires_explicit_exact_clear_grammar(text):
    assert parse_command_text(text) is None
