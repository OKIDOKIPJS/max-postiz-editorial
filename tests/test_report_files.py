"""Windows report rename retry never replays quality checks or model requests."""

import json
import os

import httpx
import pytest

from max_postiz_agent import report_files
from max_postiz_agent.config import AgentError
from test_article_benchmark_quality import add_task, execute, fake_lt, report as quality_report
from test_translation_cascade import fallback, report as cascade_report, reviewer, row

pytest_plugins = ["test_article_benchmark_quality", "test_translation_cascade"]


class Clock:
    def __init__(self):
        self.value = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(report_files, "_WINDOWS", True)
    monkeypatch.setattr(report_files, "_monotonic", value.monotonic)
    monkeypatch.setattr(report_files, "_sleep", value.sleep)
    return value


def windows_error(code):
    error = PermissionError("synthetic local rename lock")
    error.winerror = code
    return error


def files(tmp_path):
    old, pending = tmp_path / "report.json", tmp_path / "report.json.new"
    old.write_bytes(b"old durable report")
    with pending.open("wb") as stream:
        stream.write(b"unchanged fsynced report")
        stream.flush()
        os.fsync(stream.fileno())
    return old, pending


@pytest.mark.parametrize("code", [5, 32, 33])
def test_two_transient_locks_commit_same_fsynced_bytes_once(tmp_path, monkeypatch, clock, code):
    old, pending = files(tmp_path)
    attempts, commits = [], []
    original = report_files._replace

    def locked(source, destination):
        assert destination.read_bytes() == b"old durable report"
        attempts.append(source.read_bytes())
        if len(attempts) <= 2:
            raise windows_error(code)
        original(source, destination)
        commits.append(True)

    monkeypatch.setattr(report_files, "_replace", locked)
    report_files.replace_report_file(pending, old)
    assert attempts == [b"unchanged fsynced report"] * 3 and commits == [True]
    assert old.read_bytes() == b"unchanged fsynced report" and not pending.exists()
    assert clock.sleeps == [0.05, 0.05]


def test_permanent_lock_is_bounded_and_preserves_both_files(tmp_path, monkeypatch, clock):
    old, pending = files(tmp_path)
    calls, error = [], windows_error(5)

    def locked(*args):
        calls.append(args)
        raise error

    monkeypatch.setattr(report_files, "_replace", locked)
    with pytest.raises(PermissionError) as caught:
        report_files.replace_report_file(pending, old)
    assert caught.value is error and 1 < len(calls) <= 21
    assert sum(clock.sleeps) <= 1.000001 and clock.value <= 1.000001
    assert old.read_bytes() == b"old durable report" and pending.read_bytes() == b"unchanged fsynced report"


@pytest.mark.parametrize("windows,code", [(False, 5), (True, 2), (True, 112), (True, None)])
def test_other_platforms_and_errors_fail_immediately(tmp_path, monkeypatch, clock, windows, code):
    old, pending = files(tmp_path)
    calls, error = [], windows_error(code)
    monkeypatch.setattr(report_files, "_WINDOWS", windows)

    def failed(*args):
        calls.append(args)
        raise error

    monkeypatch.setattr(report_files, "_replace", failed)
    with pytest.raises(PermissionError) as caught:
        report_files.replace_report_file(pending, old)
    assert caught.value is error and len(calls) == 1 and clock.sleeps == []
    assert old.read_bytes() == b"old durable report" and pending.read_bytes() == b"unchanged fsynced report"


def test_overslept_deadline_never_starts_another_replace(tmp_path, monkeypatch, clock):
    old, pending = files(tmp_path)
    calls, error = [], windows_error(32)

    def failed(*args):
        calls.append(args)
        raise error

    monkeypatch.setattr(report_files, "_replace", failed)
    monkeypatch.setattr(report_files, "_sleep", lambda seconds: setattr(clock, "value", 2.0))
    with pytest.raises(PermissionError):
        report_files.replace_report_file(pending, old)
    assert len(calls) == 1 and pending.exists() and old.read_bytes() == b"old durable report"


def lock_completed_report(monkeypatch, *, permanently=False):
    original, attempts = report_files._replace, []

    def locked(source, destination):
        saved = json.loads(source.read_text(encoding="utf-8"))
        completed = saved.get("events", []) and saved["events"][-1]["kind"] == "lt_completed"
        if completed:
            attempts.append(source.read_bytes())
            if permanently or len(attempts) <= 2:
                raise windows_error(5)
        return original(source, destination)

    monkeypatch.setattr(report_files, "_replace", locked)
    return attempts


def test_post_lt_rename_retry_keeps_one_http_call_and_input_history(checker, sample, monkeypatch, clock):
    add_task(checker, sample)
    frozen = (sample["corpus"].read_bytes(), sample["journal"].read_bytes())
    attempts = lock_completed_report(monkeypatch)
    calls = []
    execute(checker, sample, calls)
    prefix = quality_report(sample)["events"]
    execute(checker, sample, calls, resume=True)
    assert len(calls) == 1 and len(attempts) == 3 and len(set(attempts)) == 1
    assert quality_report(sample)["events"][:len(prefix)] == prefix
    assert (sample["corpus"].read_bytes(), sample["journal"].read_bytes()) == frozen


def test_exhausted_post_lt_save_preserves_unknown_claim_and_never_repeats_http(checker, sample, monkeypatch, clock):
    add_task(checker, sample)
    frozen = (sample["corpus"].read_bytes(), sample["journal"].read_bytes())
    attempts = lock_completed_report(monkeypatch, permanently=True)
    calls = []
    with pytest.raises(PermissionError):
        execute(checker, sample, calls)
    durable = sample["output"].read_bytes()
    pending = sample["output"].with_suffix(".json.new")
    assert json.loads(durable)["events"][-1]["kind"] == "lt_started"
    assert json.loads(pending.read_bytes())["events"][-1]["kind"] == "lt_completed"
    assert 1 < len(attempts) <= 21 and len(set(attempts)) == 1 and len(calls) == 1
    monkeypatch.setattr(report_files, "_replace", os.replace)
    execute(checker, sample, calls, resume=True)
    assert len(calls) == 1 and quality_report(sample)["events"][-1]["language_tool"]["status"] == "unknown"
    assert (sample["corpus"].read_bytes(), sample["journal"].read_bytes()) == frozen


def test_changed_input_during_lt_still_blocks_next_http(checker, sample, monkeypatch, clock):
    add_task(checker, sample)
    add_task(checker, sample, ident="A02")
    lock_completed_report(monkeypatch)
    calls, normal = [], fake_lt([])

    def change(request):
        calls.append(str(request.url))
        response = normal.handle_request(request)
        sample["corpus"].write_bytes(sample["corpus"].read_bytes() + b" ")
        return response

    with pytest.raises(AgentError, match="BENCHMARK_QUALITY_INPUT_HISTORY_CHANGED"):
        checker.run(sample["root"], sample["corpus"], sample["journal"], sample["output"],
                    transport=httpx.MockTransport(change))
    assert len(calls) == 1


def test_cascade_post_review_rename_does_not_repeat_model_or_fallback(cascade, tmp_path, monkeypatch, clock):
    saved = cascade_report(cascade, tmp_path)
    original, attempts = report_files._replace, []

    def locked(source, destination):
        value = json.loads(source.read_bytes())
        completed = any(item.get("status") == "approved" for item in value["reviews"].values())
        if completed:
            attempts.append(source.read_bytes())
            if len(attempts) <= 2:
                raise windows_error(33)
        return original(source, destination)

    monkeypatch.setattr(report_files, "_replace", locked)
    reviews, translations = [], []
    model, translator = reviewer(cascade, ["approved"], reviews), fallback(translations)
    cascade.run_case(saved, model, translator, "Frozen instruction", row())
    cascade.run_case(saved, model, translator, "Frozen instruction", row())
    assert len(reviews) == 1 and translations == []
    assert attempts[0] == attempts[1] == attempts[2]
