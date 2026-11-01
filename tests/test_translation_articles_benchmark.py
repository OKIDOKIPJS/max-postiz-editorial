"""Стенд сохраняет полные данные и возобновляется без моделей, сети и публикаций."""

import base64
import copy
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.reddit import text_hash


@pytest.fixture
def bench():
    path = Path(__file__).resolve().parents[1] / "deployment/translation/benchmark_articles.py"
    spec = importlib.util.spec_from_file_location("translation_articles_benchmark_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def inputs(tmp_path, bench):
    data = tmp_path / "data"
    data.mkdir()
    articles = []
    for number in range(20):
        ident = f"A{number + 1:02}"
        original = (f"Source {ident} measured 2.5 km. "
                    + "The telescope collects observations of distant galaxies and cosmic gas for the scientific team. " * 12)
        article = {"id": ident, "source_id": "test_science", "title": "Title " + ident,
                   "original": original, "url": "https://science.example.org/" + ident,
                   "published_at": 1792000000, "text_hash": text_hash(original),
                   "word_count": len(original.split()), "provenance": {"kind": "synthetic full fixture"}}
        article["source_hash"] = fingerprint({"title": article["title"], "body": original, "url": article["url"]})
        article["original_sha256"] = bench.sha(original.encode())
        articles.append(article)
    corpus = {"schema_version": "translation-comparison-corpus-v1", "frozen_at": "test", "articles": articles}
    routes = []
    for number, ident in enumerate(bench.ROUTES):
        routes.append({"id": ident, "base_url": f"http://127.0.0.1:{9100 + number}",
            "model": bench.HY_MODEL if ident.startswith("hy_") else ident,
            "profile": {"backend_identity": "same-hy-weights" if ident.startswith("hy_") else ident,
                "sampling": {"temperature": 0, "max_tokens": 1024}, "gateway_numeric_restore": False,
                "prompt_sha256": str(number + 1) * 64, "cache_policy": "backend prompt cache retained",
                **({"glossary_sha256": "a" * 64} if ident == "hy_glossary" else {})}})
    corpus_path, routes_path = data / "corpus.local.json", data / "routes.local.json"
    corpus_path.write_text(json.dumps(corpus, ensure_ascii=False), encoding="utf-8")
    routes_path.write_text(json.dumps({"schema_version": "translation-comparison-routes-v1", "routes": routes}), encoding="utf-8")
    return tmp_path, corpus_path, routes_path, corpus, routes


def plan(bench, inputs):
    return bench.load_plan(*inputs[:3])


def events(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def fake_gateway(calls, *, cold=None, failure=None):
    def handle(request):
        payload = json.loads(request.content)
        calls.append((str(request.url), payload))
        if failure is not None:
            return failure(request)
        text = payload["text"].replace("Title", "Заголовок").replace("Source", "Источник").replace("2.5", "2,5")
        metrics = {"inference_seconds": 0.01, "load_seconds": 0}
        if cold is not None:
            metrics["cold_start"] = cold
        return httpx.Response(200, json={"model": payload["model"], "text": text,
            "complete": True, "metrics": metrics})
    return lambda: httpx.MockTransport(handle)


@pytest.mark.parametrize("route_id", ["hy_glossary", "madlad400_3b", "translategemma"])
def test_gateway_adapter_preflights_both_fields_without_model_request(bench, inputs, route_id):
    route = plan(bench, inputs)["routes"][route_id]
    calls = []
    adapter = bench.BenchmarkGatewayTranslator(route, fake_gateway(calls)())
    try:
        with pytest.raises(AgentError, match="TRANSLATION_PREFLIGHT_UNSPLITTABLE_INPUT") as failed:
            adapter.generate_text(json.dumps({"source_untrusted": {"title": "Title", "body": "X" * 1401}}))
        evidence = failed.value.preflight_evidence
        assert evidence["model"] == route["model"] and evidence["segment_chars"] == 1400
        assert evidence["config_hash"] == fingerprint({
            "model": route["model"], "base_url": route["base_url"].rstrip("/"), "segment_chars": 1400,
        })
        assert evidence["no_model_requests"] is True and not calls
    finally:
        adapter.client.close()


def test_dry_run_reports_all_80_tasks_and_real_segment_count_without_http_or_output(bench, inputs, capsys):
    root, corpus, routes, _, _ = inputs
    output = root / "data/report.local.jsonl"
    result = bench.main(["--root", str(root), "--corpus", str(corpus), "--routes", str(routes), "--output", str(output)])
    assert result["dry_run"] and result["article_route_tasks"] == 80
    expected = sum(len(bench.segment_plan(article)) for article in inputs[3]["articles"])
    assert result["http_calls_per_route"] == expected and result["http_calls_total"] == expected * 4
    assert not output.exists() and "original" not in capsys.readouterr().out


@pytest.mark.parametrize("route", ["hy_current", "hy_glossary", "madlad400_3b", "translategemma"])
def test_each_route_uses_full_production_segment_adapter_and_audits_raw_numeric_changes(bench, inputs, route):
    value, calls = plan(bench, inputs), []
    output = inputs[0] / "data/report.local.jsonl"
    original = copy.deepcopy(value["articles"])
    result = bench.run(value, output, route_id=route, transport_factory=fake_gateway(calls, cold=False))
    saved = events(output)
    assert result[route]["completed"] == 20 and result[route]["errors"] == 0
    assert result[route]["accuracy_evaluated"] is False and result[route]["warm_median_seconds"] is not None
    completed = [e for e in saved if e["kind"] == "task_completed"]
    assert [e["article"] for e in completed] == [a["id"] for a in original]
    expected = [segment["text"] for article in original for segment in bench.segment_plan(article)]
    assert [payload["text"] for _, payload in calls] == expected
    assert all(len(text) <= 1400 for text in expected)
    assert all(set(payload) == {"model", "text", "source", "target"} for _, payload in calls)
    assert all(url == value["routes"][route]["base_url"] + "/translate" for url, _ in calls)
    raw_rows = [e for e in saved if e["kind"] == "segment_returned"]
    assert len(raw_rows) == len(calls)
    for event in raw_rows:
        raw = base64.b64decode(event["raw_response_base64"])
        assert bench.sha(raw) == event["raw_sha256"]
    changed = [e for e in saved if e["kind"] == "numeric_postprocessing" and e["replacements"]]
    assert changed and all("2,5" in e["raw_model_text"] and "2.5" in e["adapter_text"] for e in changed)
    assert all(e["model_quality_improvement"] is False and e["changes"] for e in changed)
    assert all("2.5" in e["adapter_output"]["body"] for e in completed)
    assert value["articles"] == original
    assert all(e.get("accuracy_evaluated") is not True for e in saved)


def test_resume_never_repeats_a_completed_task_and_appends_next_route(bench, inputs):
    value, calls = plan(bench, inputs), []
    output = inputs[0] / "data/report.local.jsonl"
    factory = fake_gateway(calls)
    bench.run(value, output, route_id="hy_current", transport_factory=factory)
    prefix, count = output.read_bytes(), len(calls)
    result = bench.run(value, output, route_id="hy_current", resume=True, transport_factory=factory)
    assert len(calls) == count and result["hy_current"]["completed"] == 20
    assert result["hy_current"]["warm_median_seconds"] is None
    assert result["hy_current"]["after_first_wall_median_seconds"] is not None
    bench.run(value, output, route_id="hy_glossary", resume=True, transport_factory=factory)
    assert output.read_bytes().startswith(prefix) and len(calls) == count * 2
    starts = [e for e in events(output) if e["kind"] == "task_started"]
    assert len({e["task"] for e in starts}) == 40
    assert [e["route"] for e in starts] == ["hy_current"] * 20 + ["hy_glossary"] * 20


def test_bounded_partial_pass_resumes_remaining_articles_without_repeating_completed_calls(bench, inputs):
    value, calls = plan(bench, inputs), []
    output = inputs[0] / "data/report.local.jsonl"
    first = bench.run(value, output, route_id="hy_current", max_new_articles=3, transport_factory=fake_gateway(calls))
    assert first["hy_current"]["completed"] == 3
    prefix, count = output.read_bytes(), len(calls)
    final = bench.run(value, output, route_id="hy_current", resume=True, transport_factory=fake_gateway(calls))
    assert final["hy_current"]["completed"] == 20 and output.read_bytes().startswith(prefix)
    assert len(calls) > count
    starts = [e["task"] for e in events(output) if e["kind"] == "task_started"]
    assert len(starts) == len(set(starts)) == 20


def test_changed_corpus_during_a_pass_stops_before_starting_another_article(bench, inputs):
    value, calls = plan(bench, inputs), []
    output = inputs[0] / "data/report.local.jsonl"
    good = fake_gateway(calls)()

    def change_after_first_request(request):
        response = good.handle_request(request)
        inputs[1].write_bytes(inputs[1].read_bytes() + b" ")
        return response

    with pytest.raises(AgentError, match="BENCHMARK_FROZEN_INPUT_CHANGED"):
        bench.run(value, output, route_id="hy_current", transport_factory=lambda: httpx.MockTransport(change_after_first_request))
    starts = [e for e in events(output) if e["kind"] == "task_started"]
    assert len(starts) == 1 and starts[0]["article"] == "A01"


def test_second_runner_cannot_share_the_same_report_lock(bench, inputs):
    value, calls = plan(bench, inputs), []
    output = inputs[0] / "data/report.local.jsonl"
    with bench.profile_lock(output.with_suffix(output.suffix + ".lock")):
        with pytest.raises(AgentError, match="BROWSER_PROFILE_BUSY"):
            bench.run(value, output, route_id="hy_current", transport_factory=fake_gateway(calls))
    assert not calls and not output.exists()


@pytest.mark.parametrize("failure", ["timeout", "server_error", "interruption"])
def test_uncertain_or_interrupted_call_is_preserved_and_never_repeated_on_resume(bench, inputs, failure):
    value, calls = plan(bench, inputs), []
    output = inputs[0] / "data/report.local.jsonl"

    def failed(request):
        if failure == "interruption":
            raise KeyboardInterrupt
        if failure == "timeout":
            raise httpx.ReadTimeout("simulated uncertain inference", request=request)
        return httpx.Response(502, content=b'{"error":"TRANSLATION_FAILED"}')

    with pytest.raises((AgentError, KeyboardInterrupt)):
        bench.run(value, output, route_id="hy_current", transport_factory=fake_gateway(calls, failure=failed))
    before = output.read_bytes()
    assert len(calls) == 1 and any(e["kind"] == "segment_started" for e in events(output))
    if failure == "server_error":
        assert any(e["kind"] == "segment_returned" for e in events(output))
    with pytest.raises(AgentError, match="BENCHMARK_UNCERTAIN_TASK"):
        bench.run(value, output, route_id="hy_current", resume=True, transport_factory=fake_gateway(calls))
    assert output.read_bytes() == before and len(calls) == 1


def test_known_complete_invalid_response_is_recorded_without_retry_and_other_articles_continue(bench, inputs):
    value, calls = plan(bench, inputs), []
    output = inputs[0] / "data/report.local.jsonl"
    result = bench.run(value, output, route_id="madlad400_3b", transport_factory=fake_gateway(
        calls, failure=lambda _: httpx.Response(200, content=b'{"complete":false,"text":"raw incomplete"}')))
    assert result["madlad400_3b"]["errors"] == 20 and result["madlad400_3b"]["completed"] == 0
    assert len(calls) == 20
    assert all(e.get("uncertain") is False for e in events(output) if e["kind"] == "task_failed")
    count = len(calls)
    bench.run(value, output, route_id="madlad400_3b", resume=True, transport_factory=fake_gateway(calls))
    assert len(calls) == count


@pytest.mark.parametrize("reason,text", [("length", "Частичный ответ"), ("empty", "")])
def test_known_model_failure_is_measured_and_next_article_resumes_without_retry(bench, inputs, reason, text):
    value, calls = plan(bench, inputs), []
    output = inputs[0] / "data/report.local.jsonl"
    def failure(request):
        payload = json.loads(request.content)
        return httpx.Response(200, json={"model": payload["model"], "text": text, "complete": False,
            "metrics": {"known_model_failure": True, "finish_reason": reason}})
    result = bench.run(value, output, route_id="hy_current", max_new_articles=1,
                       transport_factory=fake_gateway(calls, failure=failure))
    assert result["hy_current"]["errors"] == result["hy_current"]["known_model_failures"] == 1
    assert result["hy_current"]["failed_wall_median_seconds"] is not None
    failed = next(event for event in events(output) if event["kind"] == "task_failed")
    assert failed["known_model_failure"] is True and failed["uncertain"] is False
    result = bench.run(value, output, route_id="hy_current", resume=True, max_new_articles=1,
                       transport_factory=fake_gateway(calls))
    assert result["hy_current"]["completed"] == 1 and result["hy_current"]["errors"] == 1
    terminal = [event for event in events(output) if event["kind"] in {"task_completed", "task_failed"}]
    assert [event["article"] for event in terminal] == ["A01", "A02"]
    assert len(calls) == 1 + len(bench.segment_plan(value["articles"][1]))


@pytest.mark.parametrize("damage", ["model", "complete", "marker", "reason", "nonempty", "http"])
def test_only_strict_gateway_known_model_failure_is_classified(bench, inputs, damage):
    value = plan(bench, inputs)
    output = inputs[0] / "data/report.local.jsonl"
    payload = {"model": bench.HY_MODEL, "text": "", "complete": False,
               "metrics": {"known_model_failure": True, "finish_reason": "empty"}}
    if damage == "model":
        payload["model"] = "wrong"
    elif damage == "complete":
        payload["complete"] = 0
    elif damage == "marker":
        payload["metrics"]["known_model_failure"] = 1
    elif damage == "reason":
        payload["metrics"]["finish_reason"] = "error"
    elif damage == "nonempty":
        payload["text"] = "Nonempty"
    try:
        bench.run(value, output, route_id="hy_current", max_new_articles=1, transport_factory=fake_gateway([], failure=lambda _:
                  httpx.Response(503 if damage == "http" else 200, json=payload)))
    except AgentError:
        assert damage == "http"
    failed = next(event for event in events(output) if event["kind"] == "task_failed")
    assert failed["known_model_failure"] is False


@pytest.mark.parametrize("damage", ["corpus", "routes", "journal", "partial_line"])
def test_changed_frozen_inputs_or_raw_journal_block_resume_before_any_http(bench, inputs, damage):
    value, calls = plan(bench, inputs), []
    output = inputs[0] / "data/report.local.jsonl"
    bench.run(value, output, route_id="hy_current", transport_factory=fake_gateway(calls))
    count = len(calls)
    if damage == "corpus":
        inputs[1].write_bytes(inputs[1].read_bytes() + b" ")
    elif damage == "routes":
        inputs[2].write_bytes(inputs[2].read_bytes() + b" ")
    elif damage == "journal":
        saved = events(output)
        saved[2]["source_hash"] = "0" * 64
        output.write_text("\n".join(json.dumps(e) for e in saved) + "\n", encoding="utf-8")
    else:
        output.write_bytes(output.read_bytes()[:-1])
    with pytest.raises(AgentError, match="BENCHMARK_RESUME_CONTRACT_CHANGED|BENCHMARK_JOURNAL_INVALID"):
        bench.run(plan(bench, inputs), output, route_id="hy_glossary", resume=True, transport_factory=fake_gateway(calls))
    assert len(calls) == count


@pytest.mark.parametrize("damage", ["count", "duplicate", "hash", "word_count", "excerpt", "numeric_restore", "hy_backend", "hy_prompt"])
def test_invalid_corpus_or_unfair_hy_profiles_fail_in_preflight(bench, inputs, damage):
    root, corpus_path, routes_path, corpus, routes = inputs
    if damage == "count":
        corpus["articles"].pop()
    elif damage == "duplicate":
        corpus["articles"][1] = corpus["articles"][0]
    elif damage == "hash":
        corpus["articles"][0]["text_hash"] = "0" * 64
    elif damage == "word_count":
        corpus["articles"][0]["word_count"] += 1
    elif damage == "excerpt":
        corpus["articles"][0]["excerpt"] = True
    elif damage == "numeric_restore":
        routes[2]["profile"]["gateway_numeric_restore"] = True
    elif damage == "hy_backend":
        routes[1]["profile"]["backend_identity"] += " changed weights"
    else:
        routes[1]["profile"]["prompt_sha256"] = routes[0]["profile"]["prompt_sha256"]
    corpus_path.write_text(json.dumps(corpus), encoding="utf-8")
    routes_path.write_text(json.dumps({"schema_version": "translation-comparison-routes-v1", "routes": routes}), encoding="utf-8")
    with pytest.raises(AgentError, match="BENCHMARK_INPUT_INVALID"):
        bench.load_plan(root, corpus_path, routes_path)


def test_output_and_inputs_must_stay_in_ignored_data_and_execute_requires_one_route(bench, inputs, capsys):
    value = plan(bench, inputs)
    with pytest.raises(AgentError, match="BENCHMARK_PRIVATE_PATH_REQUIRED"):
        bench.run(value, inputs[0] / "public.jsonl", route_id="hy_current")
    root, corpus, routes, _, _ = inputs
    with pytest.raises(AgentError, match="BENCHMARK_ROUTE_REQUIRED"):
        bench.main(["--root", str(root), "--corpus", str(corpus), "--routes", str(routes),
                    "--output", "data/results.local.jsonl", "--execute"])
    assert capsys.readouterr().out == "" and not (root / "data/results.local.jsonl").exists()
