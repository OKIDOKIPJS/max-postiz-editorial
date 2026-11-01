"""Офлайн-результаты стенда: production hard gates и отдельная свежая проверка LT.

Утилита не переводит, не исправляет и не согласовывает текст. Смысловую оценку
нужно выполнить отдельно; зелёные механические проверки не заменяют Ministral.
"""

import argparse
import base64
import copy
import hashlib
import json
import math
import os
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx

from max_postiz_agent import (
    article_attribution,
    article_links,
    article_quality,
    article_sources,
    collectors,
    editorial,
)
from max_postiz_agent.article_attribution import article_footer
from max_postiz_agent.article_links import REFERENCE_CONTRACT, reference_urls
from max_postiz_agent.article_quality import (
    MEANING_FIRST_REVIEW_RUBRIC,
    SPELLING_URL,
    rubric_spelling,
    rubric_translation_gate,
)
from max_postiz_agent.browser_runtime import profile_lock
from max_postiz_agent.config import AgentError, fingerprint, parse_json
from max_postiz_agent.editorial import numbers
from max_postiz_agent.reddit import text_hash
from max_postiz_agent.report_files import replace_report_file

CONTRACT = "article-benchmark-mechanical-quality-v1"
ROUTES = ("hy_current", "hy_glossary", "madlad400_3b", "translategemma")
MAX_INPUT_BYTES = 256 * 1024 * 1024


def require(condition, code="BENCHMARK_QUALITY_INPUT_INVALID"):
    if not condition:
        raise AgentError(code, 409)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def stamp():
    return datetime.now(UTC).isoformat()


def private_path(root, value):
    root, value = Path(root).resolve(), Path(value)
    result = (value if value.is_absolute() else root / value).resolve()
    require(result.is_relative_to(root / "data") and result != root / "data",
            "BENCHMARK_QUALITY_PRIVATE_PATH_REQUIRED")
    return result


def read_bytes(path):
    require(0 < path.stat().st_size <= MAX_INPUT_BYTES, "BENCHMARK_QUALITY_INPUT_TOO_LARGE")
    return path.read_bytes()


def digest(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def load_inputs(root, corpus, results):
    """Проверить сохранённый журнал, не открывая Journal, который умеет писать."""
    root = Path(root).resolve()
    corpus_path = private_path(root, corpus)
    corpus_raw = read_bytes(corpus_path)
    value = parse_json(corpus_raw.decode("utf-8"))
    require(isinstance(value, dict) and value.get("schema_version") == "translation-comparison-corpus-v1"
            and isinstance(value.get("articles"), list) and len(value["articles"]) == 20)
    articles, urls, hashes = {}, set(), set()
    for article in value["articles"]:
        require(isinstance(article, dict) and all(isinstance(article.get(k), str) and article[k].strip()
                for k in ("id", "title", "original", "url", "source_id", "text_hash")))
        require(article["id"] not in articles and article["url"] not in urls
                and article["text_hash"] not in hashes and article.get("excerpt", False) is False)
        require(article["text_hash"] == text_hash(article["original"])
                and type(article.get("word_count")) is int
                and article["word_count"] == len(article["original"].split())
                and type(article.get("published_at")) in {int, float}
                and math.isfinite(article["published_at"]) and article["published_at"] >= 0)
        source_hash = fingerprint({"title": article["title"], "body": article["original"], "url": article["url"]})
        require(article.get("source_hash", source_hash) == source_hash,
                "BENCHMARK_QUALITY_SOURCE_HASH_MISMATCH")
        require(article.get("original_sha256", sha(article["original"].encode())) == sha(article["original"].encode()),
                "BENCHMARK_QUALITY_SOURCE_HASH_MISMATCH")
        articles[article["id"]] = copy.deepcopy(article)
        urls.add(article["url"])
        hashes.add(article["text_hash"])
    paths = [results] if isinstance(results, (str, Path)) else list(results)
    require(paths)
    paths = [private_path(root, p) for p in paths]
    require(len(set(paths)) == len(paths) and corpus_path not in paths)
    tasks, snapshots, journal_contract = {}, [], None
    known = {"header", "route_session", "task_started", "segment_started", "segment_returned",
             "segment_failed", "numeric_postprocessing", "task_completed", "task_failed"}
    for path in paths:
        raw, previous, local = read_bytes(path), None, {}
        require(raw.endswith(b"\n"), "BENCHMARK_QUALITY_JOURNAL_INVALID")
        for index, line in enumerate(raw.decode("utf-8").splitlines(), 1):
            event = parse_json(line)
            require(isinstance(event, dict) and type(event.get("sequence")) is int
                    and event["sequence"] == index and event.get("previous_sha256") == previous
                    and event.get("kind") in known
                    and event.get("event_sha256") == fingerprint({k: v for k, v in event.items() if k != "event_sha256"}),
                    "BENCHMARK_QUALITY_JOURNAL_INVALID")
            previous, kind = event["event_sha256"], event["kind"]
            if index == 1:
                contract, profiles = event.get("contract"), event.get("profiles")
                require(kind == "header" and isinstance(contract, dict)
                        and contract.get("id") == "translation-articles-benchmark-v1"
                        and contract.get("corpus_sha256") == sha(corpus_raw)
                        and all(digest(contract.get(k)) for k in
                                ("corpus_sha256", "routes_sha256", "adapter_sha256", "harness_sha256"))
                        and isinstance(profiles, list) and len(profiles) == 4
                        and all(isinstance(p, dict) and isinstance(p.get("id"), str) for p in profiles)
                        and {p.get("id") for p in profiles} == set(ROUTES),
                        "BENCHMARK_QUALITY_SOURCE_HASH_MISMATCH")
                require(journal_contract is None or journal_contract == contract,
                        "BENCHMARK_QUALITY_JOURNAL_CONTRACT_CHANGED")
                journal_contract = copy.deepcopy(contract)
                continue
            require(kind != "header", "BENCHMARK_QUALITY_JOURNAL_INVALID")
            if kind == "route_session":
                require(event.get("route") in ROUTES, "BENCHMARK_QUALITY_JOURNAL_INVALID")
                continue
            task = event.get("task")
            if kind == "task_started":
                route, ident = event.get("route"), event.get("article")
                require(route in ROUTES and isinstance(ident, str) and ident in articles and task == route + ":" + ident
                        and task not in tasks, "BENCHMARK_QUALITY_JOURNAL_INVALID")
                source = articles[ident]
                expected = fingerprint({"title": source["title"], "body": source["original"], "url": source["url"]})
                require(event.get("source_hash") == expected, "BENCHMARK_QUALITY_SOURCE_HASH_MISMATCH")
                local[task] = {"start": event, "path": str(path), "status": "started"}
                tasks[task] = local[task]
                continue
            require(isinstance(task, str) and task in local and local[task]["status"] == "started",
                    "BENCHMARK_QUALITY_JOURNAL_INVALID")
            if kind == "segment_returned":
                try:
                    segment_raw = base64.b64decode(event["raw_response_base64"], validate=True)
                except (KeyError, ValueError, TypeError) as exc:
                    raise AgentError("BENCHMARK_QUALITY_JOURNAL_INVALID", 409) from exc
                require(sha(segment_raw) == event.get("raw_sha256"), "BENCHMARK_QUALITY_JOURNAL_INVALID")
            if kind not in {"task_completed", "task_failed"}:
                continue
            start = local[task]["start"]
            require(event.get("route") == start["route"] and event.get("article") == start["article"],
                    "BENCHMARK_QUALITY_JOURNAL_INVALID")
            if kind == "task_completed":
                candidate, candidate_raw = event.get("adapter_output"), event.get("adapter_output_json")
                require(isinstance(candidate, dict) and set(candidate) == {"title", "body"}
                        and all(isinstance(v, str) and v.strip() for v in candidate.values())
                        and isinstance(candidate_raw, str) and parse_json(candidate_raw) == candidate
                        and sha(candidate_raw.encode()) == event.get("adapter_output_sha256"),
                        "BENCHMARK_QUALITY_CANDIDATE_HASH_MISMATCH")
                require(event.get("candidate_hash", fingerprint(candidate)) == fingerprint(candidate),
                        "BENCHMARK_QUALITY_CANDIDATE_HASH_MISMATCH")
            local[task].update(status=kind, terminal=event)
        require(previous is not None, "BENCHMARK_QUALITY_JOURNAL_INVALID")
        snapshots.append({"path": str(path), "bytes": len(raw), "sha256": sha(raw), "head": previous})
    return {"root": root, "corpus": corpus_path, "corpus_sha256": sha(corpus_raw),
            "articles": articles, "tasks": tasks, "snapshots": snapshots,
            "journal_contract": journal_contract}


def contract(plan):
    modules = (article_quality, article_links, article_attribution, article_sources, editorial, collectors)
    return {"id": CONTRACT, "corpus_sha256": plan["corpus_sha256"],
            "benchmark_contract": plan["journal_contract"], "checker_sha256": sha(Path(__file__).read_bytes()),
            "production_modules": {m.__name__: sha(Path(m.__file__).read_bytes()) for m in modules},
            "rubric": MEANING_FIRST_REVIEW_RUBRIC, "reference_contract": REFERENCE_CONTRACT,
            "spelling_url": SPELLING_URL, "allow_spacetime": True}


def save_report(path, report):
    report["report_sha256"] = fingerprint({k: v for k, v in report.items() if k != "report_sha256"})
    raw = (json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()
    temporary = path.with_suffix(path.suffix + ".new")
    descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    replace_report_file(temporary, path)
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def append_event(path, report, kind, **values):
    previous = report["events"][-1]["event_sha256"] if report["events"] else None
    event = {"sequence": len(report["events"]) + 1, "previous_sha256": previous,
             "kind": kind, "at": stamp(), **copy.deepcopy(values)}
    event["event_sha256"] = fingerprint(event)
    report["events"].append(event)
    save_report(path, report)
    return event


def load_report(path, plan, resume):
    if not path.exists():
        require(not resume, "BENCHMARK_QUALITY_RESUME_MISSING")
        return {"schema_version": CONTRACT, "contract": contract(plan), "created_at": stamp(),
                "events": [], "semantic_evaluation_pending": True,
                "model_requests": 0, "database_mutations": 0, "publication_mutations": 0}
    require(resume, "BENCHMARK_QUALITY_OUTPUT_EXISTS_USE_RESUME")
    report = parse_json(read_bytes(path).decode("utf-8"))
    require(isinstance(report, dict) and report.get("schema_version") == CONTRACT
            and report.get("contract") == contract(plan) and isinstance(report.get("events"), list)
            and report.get("semantic_evaluation_pending") is True
            and all(type(report.get(k)) is int and report[k] == 0 for k in
                    ("model_requests", "database_mutations", "publication_mutations"))
            and report.get("report_sha256") == fingerprint({k: v for k, v in report.items() if k != "report_sha256"}),
            "BENCHMARK_QUALITY_REPORT_INVALID")
    previous = None
    current = {s["path"]: s for s in plan["snapshots"]}
    for index, event in enumerate(report["events"], 1):
        require(isinstance(event, dict) and event.get("sequence") == index
                and event.get("kind") in {"input_snapshot", "lt_started", "lt_completed", "task_evaluated"}
                and event.get("previous_sha256") == previous
                and event.get("event_sha256") == fingerprint({k: v for k, v in event.items() if k != "event_sha256"}),
                "BENCHMARK_QUALITY_REPORT_INVALID")
        previous = event["event_sha256"]
        if event.get("kind") == "input_snapshot":
            for old in event["snapshots"]:
                require(old["path"] in current and type(old["bytes"]) is int and old["bytes"] > 0
                        and old["bytes"] <= current[old["path"]]["bytes"] and digest(old["sha256"]),
                        "BENCHMARK_QUALITY_INPUT_HISTORY_CHANGED")
                raw = read_bytes(Path(old["path"]))
                require(sha(raw[:old["bytes"]]) == old["sha256"], "BENCHMARK_QUALITY_INPUT_HISTORY_CHANGED")
    return report


def verify_frozen_inputs(plan):
    """Изменившийся файл останавливает проход до следующего HTTP, без повтора."""
    require(sha(read_bytes(plan["corpus"])) == plan["corpus_sha256"],
            "BENCHMARK_QUALITY_INPUT_HISTORY_CHANGED")
    for snapshot in plan["snapshots"]:
        require(sha(read_bytes(Path(snapshot["path"]))) == snapshot["sha256"],
                "BENCHMARK_QUALITY_INPUT_HISTORY_CHANGED")


def verify_lt_result(result):
    require(isinstance(result, dict) and result.get("status") in {"checked", "unavailable"},
            "BENCHMARK_QUALITY_REPORT_INVALID")
    if "raw_response_base64" in result:
        try:
            raw = base64.b64decode(result["raw_response_base64"], validate=True)
        except (ValueError, TypeError) as exc:
            raise AgentError("BENCHMARK_QUALITY_REPORT_INVALID", 409) from exc
        require(sha(raw) == result.get("raw_sha256"), "BENCHMARK_QUALITY_REPORT_INVALID")
    if result["status"] == "checked":
        require("raw_response_base64" in result and result.get("error") is None
                and type(result.get("http_status")) is int and 200 <= result["http_status"] < 300
                and isinstance(result.get("response_json"), dict)
                and parse_json(raw.decode("utf-8")) == result["response_json"]
                and isinstance(result.get("matches_raw"), list)
                and result["matches_raw"] == result["response_json"].get("matches")
                and all(isinstance(m, dict) for m in result["matches_raw"]),
                "BENCHMARK_QUALITY_REPORT_INVALID")
    else:
        require(result.get("error") == "ARTICLE_SPELLING_UNAVAILABLE", "BENCHMARK_QUALITY_REPORT_INVALID")


def check_text(source, candidate):
    original = source["title"] + "\n\n" + source["original"]
    translated = candidate["title"] + "\n\n" + candidate["body"]
    error = None
    try:
        rubric_translation_gate(original, translated, MEANING_FIRST_REVIEW_RUBRIC,
                                reference_contract=REFERENCE_CONTRACT)
    except AgentError as exc:
        error = exc.code
    en_numbers, ru_numbers = numbers(original), numbers(translated)
    en_urls = reference_urls(original, contract=REFERENCE_CONTRACT)
    ru_urls = reference_urls(translated, contract=REFERENCE_CONTRACT)
    footer = article_footer(source["url"])
    production_body = translated + "\n\n" + footer
    return {"hard_gates": {"passed": error is None, "error": error},
            "numbers": {"source": sorted(en_numbers), "candidate": sorted(ru_numbers),
                        "missing": sorted(en_numbers - ru_numbers), "unexpected": sorted(ru_numbers - en_numbers)},
            "references": {"contract": REFERENCE_CONTRACT, "source": sorted(en_urls), "candidate": sorted(ru_urls),
                           "missing": sorted(en_urls - ru_urls), "unexpected": sorted(ru_urls - en_urls)},
            "production_fit": {"fits": len(production_body) <= 3500, "characters": len(production_body),
                               "limit": 3500, "footer_sha256": sha(footer.encode())}}


def language_tool(client, text):
    result = {"status": "unavailable", "error": "ARTICLE_SPELLING_UNAVAILABLE"}
    try:
        response = client.post(SPELLING_URL, data={"language": "ru-RU", "text": text})
        raw = response.content
        result.update(http_status=response.status_code, raw_response_base64=base64.b64encode(raw).decode("ascii"),
                      raw_sha256=sha(raw))
        response.raise_for_status()
        payload = response.json()
        matches = payload["matches"]
        require(isinstance(matches, list) and all(isinstance(m, dict) for m in matches))
        return {**result, "status": "checked", "error": None, "response_json": payload,
                "matches_raw": copy.deepcopy(matches)}
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AgentError) as exc:
        return {**result, "error_type": type(exc).__name__}


def completed_checks(source, event, raw_lt):
    """Чистый пересчёт позволяет проверить кэш без нового HTTP при --resume."""
    candidate = event["adapter_output"]
    candidate_hash = fingerprint(candidate)
    translated = candidate["title"] + "\n\n" + candidate["body"]
    diagnostics = check_text(source, candidate)
    spelling = {"status": raw_lt["status"], "raw_candidate_hash": candidate_hash}
    if raw_lt["status"] == "checked":
        # Кэшируем только raw LT по точному RU. Source-bound правила
        # пересчитываются по EN каждой задачи, не переносятся между статьями.
        adjudicated, unresolved = rubric_spelling(source["title"] + "\n\n" + source["original"],
            translated, raw_lt["matches_raw"], MEANING_FIRST_REVIEW_RUBRIC, allow_spacetime=True)
        spelling.update(matches_raw=copy.deepcopy(raw_lt["matches_raw"]),
                        adjudicated=adjudicated, unresolved=unresolved)
    else:
        spelling["error"] = raw_lt["error"]
    mechanical = (diagnostics["hard_gates"]["passed"] and not spelling["unresolved"]
                  if spelling["status"] == "checked" else None)
    return {"candidate_hash": candidate_hash, "adapter_output_sha256": event["adapter_output_sha256"],
            "translation_status": "completed", **diagnostics, "language_tool": spelling,
            "mechanical_checks_passed": mechanical, "semantic_evaluation_pending": True}


def failed_checks(event):
    return {"translation_status": "failed", "candidate_hash": None,
            "upstream_error": event.get("error_code"),
            "known_model_failure": event.get("known_model_failure") is True,
            "uncertain": event.get("uncertain") is True, "semantic_evaluation_pending": True}


def summary(plan, report):
    expected = {route + ":" + ident for route in ROUTES for ident in plan["articles"]}
    terminal = {task for task, value in plan["tasks"].items() if value["status"] != "started"}
    checked = {e["task"]: e for e in report["events"] if e["kind"] == "task_evaluated"}
    uncertain = sorted(t for t in terminal if plan["tasks"][t]["terminal"].get("uncertain") is True)
    failed_unverified = sorted(t for t in terminal if plan["tasks"][t]["status"] == "task_failed"
        and (plan["tasks"][t]["terminal"].get("known_model_failure") is not True
             or plan["tasks"][t]["terminal"].get("uncertain") is not False))
    unverified = sorted(t for t, e in checked.items() if e["translation_status"] == "completed"
                        and e["language_tool"]["status"] != "checked")
    complete = (terminal == expected and set(checked) == terminal
                and not uncertain and not failed_unverified and not unverified)
    return {"expected_tasks": 80, "terminal_tasks": len(terminal), "evaluated_tasks": len(checked),
            "translation_completed": sum(v["status"] == "task_completed" for v in plan["tasks"].values()),
            "translation_failed": sum(v["status"] == "task_failed" for v in plan["tasks"].values()),
            "missing_terminal_tasks": sorted(expected - terminal), "uncertain_tasks": uncertain,
            "upstream_unverified_tasks": failed_unverified,
            "lt_unverified_tasks": unverified, "coverage_complete": terminal == expected,
            "partial": not complete, "semantic_evaluation_pending": True,
            "model_requests": 0, "database_mutations": 0, "publication_mutations": 0}


def run(root, corpus, results, output, *, resume=False, transport=None):
    plan = load_inputs(root, corpus, results)
    output = private_path(plan["root"], output)
    require(output not in {plan["corpus"], *(Path(s["path"]) for s in plan["snapshots"])},
            "BENCHMARK_QUALITY_OUTPUT_COLLISION")
    output.parent.mkdir(parents=True, exist_ok=True)
    with profile_lock(output.with_suffix(output.suffix + ".lock")):
        report = load_report(output, plan, resume)
        old_tasks, lt, lt_text_hashes = {}, {}, {}
        for event in report["events"]:
            if event["kind"] == "task_evaluated":
                require(event["task"] not in old_tasks, "BENCHMARK_QUALITY_REPORT_INVALID")
                old_tasks[event["task"]] = event
            elif event["kind"] == "lt_started":
                require(event["candidate_hash"] not in lt, "BENCHMARK_QUALITY_REPORT_INVALID")
                require(digest(event["candidate_hash"]) and digest(event.get("text_sha256")),
                        "BENCHMARK_QUALITY_REPORT_INVALID")
                lt_text_hashes[event["candidate_hash"]] = event["text_sha256"]
                lt[event["candidate_hash"]] = {"status": "unknown", "error": "LANGUAGETOOL_OUTCOME_UNKNOWN"}
            elif event["kind"] == "lt_completed":
                require(event["candidate_hash"] in lt and lt[event["candidate_hash"]]["status"] == "unknown",
                        "BENCHMARK_QUALITY_REPORT_INVALID")
                verify_lt_result(event["result"])
                lt[event["candidate_hash"]] = event["result"]
        append_event(output, report, "input_snapshot", snapshots=plan["snapshots"])
        with httpx.Client(timeout=60, trust_env=False, follow_redirects=False, transport=transport) as client:
            for task, item in plan["tasks"].items():
                if item["status"] == "started":
                    continue
                event, start = item["terminal"], item["start"]
                binding = {"task": task, "route": start["route"], "article": start["article"],
                           "source_hash": start["source_hash"], "terminal_event_sha256": event["event_sha256"]}
                if task in old_tasks:
                    require(all(old_tasks[task].get(k) == v for k, v in binding.items()),
                            "BENCHMARK_QUALITY_INPUT_HISTORY_CHANGED")
                    if event["kind"] == "task_completed":
                        candidate_hash = fingerprint(event["adapter_output"])
                        translated = event["adapter_output"]["title"] + "\n\n" + event["adapter_output"]["body"]
                        require(candidate_hash in lt and lt_text_hashes[candidate_hash] == sha(translated.encode()),
                                "BENCHMARK_QUALITY_REPORT_INVALID")
                        checks = completed_checks(plan["articles"][start["article"]], event, lt[candidate_hash])
                    else:
                        checks = failed_checks(event)
                    saved_checks = {k: v for k, v in old_tasks[task].items() if k not in
                                    {"sequence", "previous_sha256", "kind", "at", "event_sha256"}}
                    require(saved_checks == {**binding, **checks}, "BENCHMARK_QUALITY_REPORT_INVALID")
                    continue
                if event["kind"] == "task_failed":
                    append_event(output, report, "task_evaluated", **binding, **failed_checks(event))
                    continue
                candidate = event["adapter_output"]
                candidate_hash = fingerprint(candidate)
                source = plan["articles"][start["article"]]
                translated = candidate["title"] + "\n\n" + candidate["body"]
                verify_frozen_inputs(plan)
                if candidate_hash not in lt:
                    # Durable claim фиксируется ДО LT: неизвестный исход не даёт
                    # повторного HTTP при --resume даже после аварии процесса.
                    append_event(output, report, "lt_started", candidate_hash=candidate_hash,
                                 text_sha256=sha(translated.encode()))
                    lt_text_hashes[candidate_hash] = sha(translated.encode())
                    lt[candidate_hash] = language_tool(client, translated)
                    append_event(output, report, "lt_completed", candidate_hash=candidate_hash,
                                 result=lt[candidate_hash])
                require(lt_text_hashes[candidate_hash] == sha(translated.encode()),
                        "BENCHMARK_QUALITY_REPORT_INVALID")
                append_event(output, report, "task_evaluated", **binding,
                             **completed_checks(source, event, lt[candidate_hash]))
        report["summary"] = summary(plan, report)
        save_report(output, report)
        return copy.deepcopy(report["summary"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--results", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--execute", action="store_true", help="Разрешить только запросы LanguageTool")
    args = parser.parse_args(argv)
    if args.execute:
        result = run(args.root, args.corpus, args.results, args.output, resume=args.resume)
    else:
        plan = load_inputs(args.root, args.corpus, args.results)
        private_path(plan["root"], args.output)
        result = {"dry_run": True, **summary(plan, {"events": []})}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    main()
