"""Изолированное сравнение четырёх переводчиков; без публикаций и управления моделями."""

import argparse
import base64
import copy
import hashlib
import json
import math
import os
import re
import statistics
import time
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from max_postiz_agent.browser_runtime import profile_lock
from max_postiz_agent.config import AgentError, fingerprint, parse_json
from max_postiz_agent.reddit import text_hash
from max_postiz_agent.translation import DedicatedTranslator, restore_numeric_spelling, segments

CONTRACT = "translation-articles-benchmark-v1"
ROUTES = ("hy_current", "hy_glossary", "madlad400_3b", "translategemma")
HY_MODEL = "hy-mt2-7b-q8"


def require(condition, code="BENCHMARK_INPUT_INVALID"):
    if not condition:
        raise AgentError(code, 409)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def stamp():
    return datetime.now(UTC).isoformat()


def private_path(root, path):
    """Корпус, настройки стенда и полные ответы находятся только в ignored data."""
    root = Path(root).resolve()
    path = Path(path)
    path = (path if path.is_absolute() else root / path).resolve()
    require(path.is_relative_to(root / "data") and path != root / "data", "BENCHMARK_PRIVATE_PATH_REQUIRED")
    return path


def digest_value(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def segment_plan(article):
    return [{"field": field, "text": part} for field, text in
            (("title", article["title"]), ("body", article["original"]))
            for part, translate in segments(text) if translate]


def load_plan(root, corpus_path, routes_path):
    root = Path(root).resolve()
    corpus_path, routes_path = (private_path(root, path) for path in (corpus_path, routes_path))
    corpus_raw, routes_raw = corpus_path.read_bytes(), routes_path.read_bytes()
    corpus, routes = parse_json(corpus_raw.decode("utf-8")), parse_json(routes_raw.decode("utf-8"))
    require(isinstance(corpus, dict) and corpus.get("schema_version") == "translation-comparison-corpus-v1"
            and isinstance(corpus.get("articles"), list) and len(corpus["articles"]) == 20)
    seen_ids, seen_urls, seen_hashes = set(), set(), set()
    for article in corpus["articles"]:
        require(isinstance(article, dict) and all(isinstance(article.get(key), str) and article[key].strip()
                for key in ("id", "source_id", "title", "original", "url", "text_hash")))
        require(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", article["id"]) and article.get("excerpt", False) is False)
        require(article["id"] not in seen_ids and article["url"] not in seen_urls
                and article["text_hash"] not in seen_hashes)
        require(article["text_hash"] == text_hash(article["original"])
                and type(article.get("word_count")) is int and article["word_count"] == len(article["original"].split())
                and type(article.get("published_at")) in {int, float}
                and math.isfinite(article["published_at"]) and article["published_at"] >= 0)
        if "source_hash" in article:
            require(article["source_hash"] == fingerprint({"title": article["title"], "body": article["original"], "url": article["url"]}))
        if "original_sha256" in article:
            require(article["original_sha256"] == sha(article["original"].encode("utf-8")))
        segment_plan(article)  # Полный хвост должен помещаться в тот же adapter, что и production Hy.
        seen_ids.add(article["id"])
        seen_urls.add(article["url"])
        seen_hashes.add(article["text_hash"])
    require(isinstance(routes, dict) and routes.get("schema_version") == "translation-comparison-routes-v1"
            and isinstance(routes.get("routes"), list) and len(routes["routes"]) == 4)
    by_id = {}
    for route in routes["routes"]:
        require(isinstance(route, dict) and set(route) == {"id", "base_url", "model", "profile"}
                and route["id"] in ROUTES and route["id"] not in by_id)
        require(isinstance(route["base_url"], str) and isinstance(route["model"], str) and route["model"].strip())
        try:
            url = urlsplit(route["base_url"])
            valid_url = (url.scheme in {"http", "https"} and url.hostname and url.path in {"", "/"}
                         and not url.username and not url.password and not url.query and not url.fragment
                         and url.port != 0)
        except ValueError:
            valid_url = False
        require(valid_url)
        profile = route["profile"]
        require(isinstance(profile, dict) and profile.get("gateway_numeric_restore") is False
                and isinstance(profile.get("backend_identity"), str) and profile["backend_identity"].strip()
                and isinstance(profile.get("sampling"), dict)
                and isinstance(profile.get("cache_policy"), str) and profile["cache_policy"].strip())
        if route["id"] in {"hy_current", "hy_glossary"}:
            require(route["model"] == HY_MODEL and digest_value(profile.get("prompt_sha256")))
        if route["id"] == "hy_glossary":
            require(digest_value(profile.get("glossary_sha256")))
        by_id[route["id"]] = route
    current, glossary = (by_id[name] for name in ("hy_current", "hy_glossary"))
    require(current["profile"]["backend_identity"] == glossary["profile"]["backend_identity"]
            and current["profile"]["sampling"] == glossary["profile"]["sampling"]
            and current["profile"]["cache_policy"] == glossary["profile"]["cache_policy"]
            and current["profile"]["prompt_sha256"] != glossary["profile"]["prompt_sha256"])
    # Файлы и параметры замораживаются вместе; обновление adapter требует нового прогона.
    import max_postiz_agent.translation as adapter
    contract = {"id": CONTRACT, "corpus_sha256": sha(corpus_raw), "routes_sha256": sha(routes_raw),
                "adapter_sha256": sha(Path(adapter.__file__).read_bytes()),
                "harness_sha256": sha(Path(__file__).read_bytes()), "segment_limit": 1400,
                "order": "route-major; identical frozen article order for every route",
                "numeric_postprocessing": "same adapter for all routes; raw preserved separately",
                "load_measurement": "backend reports only; no model start/stop or warmup requests"}
    return {"root": root, "articles": copy.deepcopy(corpus["articles"]), "routes": by_id,
            "contract": contract, "profiles": copy.deepcopy(routes["routes"]),
            "files": {"corpus_sha256": corpus_path, "routes_sha256": routes_path}}


class Journal:
    """Дописываем fsync JSONL; незавершённый внешний вызов автоматически не повторяем."""

    def __init__(self, path, plan, resume):
        self.path, self.events, self.last_hash = path, [], None
        if path.exists():
            require(resume, "BENCHMARK_OUTPUT_EXISTS_USE_RESUME")
            raw = path.read_bytes()
            require(raw.endswith(b"\n"), "BENCHMARK_JOURNAL_INVALID")
            for line in raw.decode("utf-8").splitlines():
                event = parse_json(line)
                require(isinstance(event, dict) and event.get("sequence") == len(self.events) + 1
                        and event.get("previous_sha256") == self.last_hash, "BENCHMARK_JOURNAL_INVALID")
                content = {key: value for key, value in event.items() if key != "event_sha256"}
                require(event.get("event_sha256") == fingerprint(content), "BENCHMARK_JOURNAL_INVALID")
                self.events.append(event)
                self.last_hash = event["event_sha256"]
            require(self.events and self.events[0].get("kind") == "header"
                    and self.events[0].get("contract") == plan["contract"], "BENCHMARK_RESUME_CONTRACT_CHANGED")
        else:
            require(not resume, "BENCHMARK_RESUME_MISSING")
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            self.append("header", contract=plan["contract"], profiles=plan["profiles"], test_only=True)
        self.tasks = {}
        for event in self.events[1:]:
            key = event.get("task")
            if event["kind"] == "task_started":
                require(key not in self.tasks, "BENCHMARK_JOURNAL_INVALID")
                self.tasks[key] = event
            elif event["kind"] in {"task_completed", "task_failed"}:
                require(key in self.tasks and self.tasks[key]["kind"] == "task_started", "BENCHMARK_JOURNAL_INVALID")
                self.tasks[key] = event
        require(not any(e["kind"] == "task_started" or e.get("uncertain") is True
                        for e in self.tasks.values()), "BENCHMARK_UNCERTAIN_TASK")

    def append(self, kind, **values):
        event = {"sequence": len(self.events) + 1, "previous_sha256": self.last_hash,
                 "kind": kind, "at": stamp(), **values}
        event["event_sha256"] = fingerprint(event)
        raw = (json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        descriptor = os.open(self.path, os.O_APPEND | os.O_WRONLY)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        self.events.append(event)
        self.last_hash = event["event_sha256"]
        return event


class AuditedTransport(httpx.BaseTransport):
    def __init__(self, inner, journal, task, route, article, clock):
        self.inner, self.journal, self.task, self.route, self.clock = inner, journal, task, route, clock
        self.expected, self.calls, self.metrics, self.uncertain = segment_plan(article), 0, [], False
        self.known_model_failure = False

    def handle_request(self, request):
        data = parse_json(request.content.decode("utf-8"))
        require(request.method == "POST" and str(request.url) == self.route["base_url"].rstrip("/") + "/translate"
                and self.calls < len(self.expected) and data == {"model": self.route["model"],
            "text": self.expected[self.calls]["text"], "source": "en", "target": "ru"}, "BENCHMARK_ADAPTER_CHANGED")
        self.calls += 1
        started = self.clock()
        self.journal.append("segment_started", task=self.task, call=self.calls,
            field=self.expected[self.calls - 1]["field"], request=data,
            request_sha256=sha(request.content), endpoint=str(request.url))
        try:
            response = self.inner.handle_request(request)
            response.read()
        except Exception as exc:
            self.uncertain = True
            self.journal.append("segment_failed", task=self.task, call=self.calls,
                wall_seconds=self.clock() - started, error_type=type(exc).__name__, error=str(exc), uncertain=True)
            raise
        self.journal.append("segment_returned", task=self.task, call=self.calls,
            wall_seconds=self.clock() - started, status=response.status_code,
            raw_response_base64=base64.b64encode(response.content).decode("ascii"), raw_sha256=sha(response.content))
        self.uncertain = response.status_code >= 500
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("metrics"), dict):
            metrics = payload["metrics"]
            self.known_model_failure = (response.status_code == 200 and payload.get("model") == self.route["model"]
                and payload.get("complete") is False and isinstance(payload.get("text"), str)
                and metrics.get("known_model_failure") is True and metrics.get("finish_reason") in {"length", "empty"}
                and (metrics["finish_reason"] != "empty" or not payload["text"].strip()))
        if isinstance(payload, dict) and isinstance(payload.get("text"), str):
            raw_text = payload["text"].strip()
            restored, count = restore_numeric_spelling(data["text"], raw_text)
            changes = [{"old_start": left, "old_end": right, "new_start": start, "new_end": end,
                        "before": raw_text[left:right], "after": restored[start:end]}
                       for op, left, right, start, end in SequenceMatcher(None, raw_text, restored, autojunk=False).get_opcodes()
                       if op != "equal"]
            metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
            self.metrics.append(metrics)
            self.journal.append("numeric_postprocessing", task=self.task, call=self.calls,
                raw_model_text=raw_text, adapter_text=restored, replacements=count, changes=changes,
                raw_text_sha256=sha(raw_text.encode()), adapter_text_sha256=sha(restored.encode()),
                backend_metrics=metrics, model_quality_improvement=False)
        return response

    def close(self):
        self.inner.close()


class BenchmarkGatewayTranslator(DedicatedTranslator):
    """Тот же segment adapter; дополнительные model aliases разрешены только стенду."""

    def __init__(self, route, transport):
        self.model, self.url = route["model"], route["base_url"].rstrip("/")
        # Стенд сохраняет общий размер сегмента и proof-конфигурацию базового адаптера.
        # Дополнительные aliases не расширяют production-список разрешённых моделей.
        self.segment_chars = 1400
        self.config_hash = fingerprint({"model": self.model, "base_url": self.url,
                                        "segment_chars": self.segment_chars})
        self.client = httpx.Client(timeout=600, trust_env=False, transport=transport, follow_redirects=False)
        self._cached_source = self._cached_result = None


def summary(events):
    result = {}
    for route in ROUTES:
        rows = [e for e in events if e.get("route") == route and e["kind"] in {"task_completed", "task_failed"}]
        completed = [e for e in rows if e["kind"] == "task_completed"]
        failed = [e for e in rows if e["kind"] == "task_failed"]
        warm = [e["wall_seconds"] for e in completed if e.get("backend_reported_warm") is True]
        later = [e["wall_seconds"] for e in completed if e["position_in_route"] > 1]
        result[route] = {"completed": len(completed), "errors": len(rows) - len(completed),
            "known_model_failures": sum(e.get("known_model_failure") is True for e in failed),
            "failed_wall_median_seconds": statistics.median([e["wall_seconds"] for e in failed]) if failed else None,
            "wall_median_seconds": statistics.median([e["wall_seconds"] for e in completed]) if completed else None,
            "warm_median_seconds": statistics.median(warm) if warm else None,
            "after_first_wall_median_seconds": statistics.median(later) if later else None,
            "accuracy_evaluated": False}
    return result


def run(plan, output, *, route_id, resume=False, max_new_articles=20, transport_factory=None, clock=time.monotonic):
    require(route_id in ROUTES, "BENCHMARK_ROUTE_REQUIRED")
    require(type(max_new_articles) is int and 1 <= max_new_articles <= 20, "BENCHMARK_LIMIT_INVALID")
    output = private_path(plan["root"], output)
    # Блокировка относится только к JSONL стенда, не к browser profile или worker.
    with profile_lock(output.with_suffix(output.suffix + ".lock")):
        journal = Journal(output, plan, resume)
        route = plan["routes"][route_id]
        journal.append("route_session", route=route_id, profiles_are_declared=True, no_warmup=True,
                       max_new_articles=max_new_articles)
        new_articles = 0
        for position, article in enumerate(plan["articles"], 1):
            task = route_id + ":" + article["id"]
            if task in journal.tasks:
                continue
            if new_articles >= max_new_articles:
                break
            require(all(sha(path.read_bytes()) == plan["contract"][key] for key, path in plan["files"].items()),
                    "BENCHMARK_FROZEN_INPUT_CHANGED")
            new_articles += 1
            journal.append("task_started", task=task, route=route_id, article=article["id"],
                position_in_route=position, source_hash=fingerprint({"title": article["title"],
                    "body": article["original"], "url": article["url"]}))
            inner = transport_factory() if transport_factory else httpx.HTTPTransport(retries=0)
            audited = AuditedTransport(inner, journal, task, route, article, clock)
            translator = (DedicatedTranslator({"model": HY_MODEL, "base_url": route["base_url"]}, transport=audited)
                          if route_id == "hy_current" else BenchmarkGatewayTranslator(route, audited))
            tick = clock()
            try:
                raw, info = translator.generate_text(json.dumps({"source_untrusted": {
                    "title": article["title"], "body": article["original"], "url": article["url"]}}, ensure_ascii=False))
                translated = parse_json(raw)
                require(audited.calls == len(audited.expected) and set(translated) == {"title", "body"}
                        and all(isinstance(value, str) and value.strip() for value in translated.values()), "BENCHMARK_ADAPTER_CHANGED")
                # Warm подтверждает backend, а не порядковый номер запроса.
                warm = bool(audited.metrics) and all(m.get("cold_start") is False for m in audited.metrics)
                journal.append("task_completed", task=task, route=route_id, article=article["id"],
                    position_in_route=position, wall_seconds=clock() - tick, adapter_output=translated,
                    adapter_output_json=raw, adapter_output_sha256=sha(raw.encode()), metrics=info,
                    backend_reported_warm=warm, backend_metrics=audited.metrics, accuracy_evaluated=False)
            except Exception as exc:
                journal.append("task_failed", task=task, route=route_id, article=article["id"],
                    position_in_route=position, wall_seconds=clock() - tick,
                    error_code=exc.code if isinstance(exc, AgentError) else type(exc).__name__,
                    error=str(exc), uncertain=audited.uncertain, known_model_failure=audited.known_model_failure)
                if audited.uncertain:
                    raise AgentError("BENCHMARK_UNCERTAIN_TASK", 409) from exc
            finally:
                translator.client.close()
        return summary(journal.events)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--routes", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--only-route", choices=ROUTES)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-new-articles", type=int, default=20)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    plan = load_plan(args.root, args.corpus, args.routes)
    private_path(plan["root"], args.output)
    if args.execute:
        require(args.only_route in ROUTES, "BENCHMARK_ROUTE_REQUIRED")
        result = run(plan, args.output, route_id=args.only_route, resume=args.resume,
                     max_new_articles=args.max_new_articles)
    else:
        calls = sum(len(segment_plan(article)) for article in plan["articles"])
        result = {"dry_run": True, "contract": plan["contract"], "articles": 20,
                  "article_route_tasks": 80, "http_calls_per_route": calls, "http_calls_total": calls * 4}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    main()
