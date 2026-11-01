"""TEST ONLY: exact Argos outputs -> one review -> Hy7B fallback -> one review.

No database, drafts, scheduling or publication. Approval is a model judgement,
never proof of fidelity or permission to enable production. Resume never repeats
an uncertain translation/review call. Repeated Argos timings are not re-reviewed.
"""
import argparse
import copy
import hashlib
import json
import math
import os
import re
import statistics
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from max_postiz_agent.adapters import OpenCode, extract_object
from max_postiz_agent.cli import load_env
from max_postiz_agent.config import AgentError, Settings, fingerprint
from max_postiz_agent.report_files import replace_report_file
from max_postiz_agent.translation import DedicatedTranslator, restore_numeric_spelling

HY_MODEL = 'hy-mt2-7b-q8'
REVIEW_MODEL = 'proxmox/qwen25-coder-7b'
FULL_ID = 'nasa-escapade-full'
FALSE_CONTROL = ('Космический аппарат обнаружил жизнь. Эти наблюдения доказывают, '
                 'что Марс был обитаемым.')


def now():
    return datetime.now(ZoneInfo('Europe/Moscow')).isoformat()


def safe_error(exc):
    return exc.code if isinstance(exc, AgentError) else type(exc).__name__


def resolve(root, path):
    return (path if path.is_absolute() else root / path).resolve()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def load_inputs(corpus_path, argos_path, full_article_path=None):
    corpus_raw, argos_raw = corpus_path.read_bytes(), argos_path.read_bytes()
    try:
        corpus, packet = json.loads(corpus_raw), json.loads(argos_raw)
        if (not isinstance(corpus, dict) or not isinstance(corpus.get('cases'), list)
                or not isinstance(packet, dict) or type(packet.get('schema_version')) is not int
                or packet['schema_version'] != 1 or packet.get('status') != 'complete'
                or not isinstance(packet.get('cases'), list)
                or any(not isinstance(packet.get(key), str) or not packet[key].strip()
                       for key in ('model', 'engine'))):
            raise ValueError
        sources = {case['id']: case['text'] for case in corpus['cases']}
        if len(sources) != len(corpus['cases']):
            raise ValueError
        full_source = None
        if full_article_path is not None:
            article = json.loads(full_article_path.read_bytes())
            full_source = article['original']
            if not isinstance(full_source, str) or not full_source.strip():
                raise ValueError
            sources[FULL_ID] = full_source
        primary, repeats, seen = {}, [], set()
        for row in packet['cases']:
            if not isinstance(row, dict):
                raise ValueError
            ident, repeat = row['id'], row['repeat']
            if (not isinstance(ident, str) or type(repeat) is not int or repeat not in {1, 2}
                    or (ident, repeat) in seen or not isinstance(row.get('source'), str)
                    or type(row.get('seconds')) not in {int, float}
                    or not math.isfinite(row['seconds']) or row['seconds'] < 0):
                raise ValueError
            seen.add((ident, repeat))
            # Optional full-body data may be present even when this run excludes it.
            if ident == FULL_ID and full_source is None:
                continue
            if ident not in sources or row['source'] != sources[ident]:
                raise ValueError
            if ((not isinstance(row.get('translated'), str) or not row['translated'].strip())
                    and not row.get('error')):
                raise ValueError
            if repeat == 1:
                primary[ident] = copy.deepcopy(row)
            else:
                if ident == FULL_ID:
                    raise ValueError
                repeats.append(copy.deepcopy(row))
        required = set(sources)
        if set(primary) != required or len(repeats) != 2 or len({row['id'] for row in repeats}) != 2:
            raise ValueError
    except (ValueError, KeyError, TypeError) as exc:
        raise AgentError('CASCADE_INPUT_INVALID') from exc
    order = [case['id'] for case in corpus['cases']] + ([FULL_ID] if full_source is not None else [])
    return {'corpus': corpus, 'packet': packet, 'primary': primary, 'repeats': repeats,
            'order': order, 'corpus_sha256': digest(corpus_raw), 'argos_sha256': digest(argos_raw),
            'full_source_sha256': digest(full_source.encode()) if full_source is not None else None}


class Report:
    def __init__(self, path, contract, inputs, resume):
        self.path = path
        if path.exists():
            if not resume:
                raise AgentError('CASCADE_OUTPUT_EXISTS_USE_RESUME')
            try:
                self.value = json.loads(path.read_bytes())
                if (not isinstance(self.value, dict) or type(self.value.get('schema_version')) is not int
                        or self.value['schema_version'] != 1
                        or self.value.get('contract') != contract
                        or not isinstance(self.value.get('results'), dict)
                        or not isinstance(self.value.get('reviews'), dict)
                        or not isinstance(self.value.get('controls'), dict)):
                    raise ValueError
            except (ValueError, TypeError) as exc:
                raise AgentError('CASCADE_RESUME_CONTRACT_MISMATCH') from exc
        else:
            self.value = {'schema_version': 1, 'test_only': True, 'started_at': now(),
                'contract': contract, 'results': {}, 'reviews': {}, 'controls': {},
                'automatic_enable_performed': False, 'approval_is_proof': False,
                'review_budget': {'calls_per_exact_candidate': 1, 'repeat2_reviewed': False,
                    'server_output_token_limit_enforced_by_harness': False,
                    'prompt_requested_output_tokens': 512},
                'scope': 'Seven unique corpus texts; two repeat2 rows for Argos speed only. '
                         'Optional full NASA body is separate from the title case; '
                         'this is not the complete ArticleQuality/LanguageTool/publication gate.',
                'timing_scope': 'Recorded Argos inference plus actual Hy/review wall durations; '
                                'excludes cold model startup and offline/resume waiting. '
                                'Unknown interrupted-call durations are flagged, never estimated.',
                'argos_speed_samples': list(inputs['primary'].values()) + inputs['repeats']}
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.save()

    def save(self):
        temporary = self.path.with_suffix('.json.tmp')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(self.value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        replace_report_file(temporary, self.path)


def numeric_counts(text):
    return Counter(token.replace('−', '-').replace(' ', '')
        for token in re.findall(r'[+\-−]?[ \t]*\d+(?:[.,]\d+)*', text))


def lexical_checks(source, translated):
    """Numbers can withhold verification; units/negation counts are advisory only."""
    unit_patterns = {
        'km': r'\b(?:km|kilometers?|kilometres?|км|километр(?:а|ов|ы)?)\b',
        'miles': r'\b(?:miles?|мил(?:я|и|ь))\b',
        'kg': r'\b(?:kg|kilograms?|кг|килограмм(?:а|ов|ы)?)\b',
        'hours': r'\b(?:hours?|час(?:а|ов|ы)?)\b',
        'celsius': r'°[CС]',
    }

    def units(text):
        return {key: len(re.findall(pattern, text, re.I)) for key, pattern in unit_patterns.items()}

    original_numbers, translated_numbers = numeric_counts(source), numeric_counts(translated)
    return {'numeric_multiset_match': original_numbers == translated_numbers,
        'source_numeric_multiset': dict(original_numbers),
        'translated_numeric_multiset': dict(translated_numbers),
        'units_lexical_advisory': {'source': units(source), 'translation': units(translated)},
        'negation_lexical_advisory': {
            'source': len(re.findall(r'\b(?:not|no|never)\b', source, re.I)),
            'translation': len(re.findall(r'\b(?:не|нет|никогда)\b', translated, re.I))},
        'lexical_counts_establish_semantic_fidelity': False}


def review(report, reviewer, prompt, source, candidate):
    identity = {'source': source, 'candidate': candidate, 'review_model': reviewer.model,
                'prompt_sha256': digest(prompt.encode())}
    key = fingerprint(identity)
    cached = report.value['reviews'].get(key)
    if cached is not None:
        if any(cached.get(field) != value for field, value in identity.items()):
            raise AgentError('CASCADE_REVIEW_CACHE_BINDING_INVALID')
        if cached.get('status') == 'in_progress':
            cached.update(status='unverified', error='REVIEW_OUTCOME_UNKNOWN_NO_RESAMPLE', finished_at=now())
            report.save()
        return {**copy.deepcopy(cached), 'cache_reused': True}
    row = {**identity, 'candidate_sha256': digest(candidate.encode()),
           'cache_key': key, 'cache_reused': False, 'status': 'in_progress', 'started_at': now()}
    report.value['reviews'][key] = row
    report.save()  # Durable reservation precedes the model call.
    started = time.monotonic()
    try:
        raw, info = reviewer.generate_text(json.dumps({'instruction': prompt,
            'source_untrusted': source, 'translation_untrusted': candidate}, ensure_ascii=False))
        row.update(raw_review=raw, response_metadata=info)
        if not isinstance(raw, str) or len(raw) > 12000:
            raise AgentError('CASCADE_REVIEW_SCHEMA_INVALID')
        value = extract_object(raw)
        if (set(value) != {'verified', 'approved', 'issues'}
                or type(value['verified']) is not bool or type(value['approved']) is not bool
                or not isinstance(value['issues'], list) or len(value['issues']) > 2
                or any(not isinstance(issue, str) or not issue.strip() or len(issue) > 140
                       for issue in value['issues'])
                or (value['approved'] and (not value['verified'] or value['issues']))
                or (not value['approved'] and not value['issues'])):
            raise AgentError('CASCADE_REVIEW_SCHEMA_INVALID')
        row['parsed_review'] = value
        row['status'] = ('approved' if value['approved'] else 'rejected') if value['verified'] else 'unverified'
    except Exception as exc:
        row.update(status='unverified', error=safe_error(exc))
    finally:
        row.update(seconds=round(time.monotonic() - started, 6), finished_at=now())
        report.save()
    return copy.deepcopy(row)


def run_controls(report, reviewer, prompt, corpus):
    controlled = next(case for case in corpus['cases'] if case['id'] == 'controlled-negation-modality')
    for ident, candidate, expected in [('literal_positive', controlled['reference_ru'], 'approved'),
                                     ('deliberate_false_claim', FALSE_CONTROL, 'rejected')]:
        result = review(report, reviewer, prompt, controlled['text'], candidate)
        report.value['controls'][ident] = {'expected': expected, 'review': result,
            'passed': result['status'] == expected,
            'critical_false_approval': ident == 'deliberate_false_claim' and result['status'] == 'approved'}
        report.save()


def run_case(report, reviewer, fallback, prompt, argos_row):
    ident, source = argos_row['id'], argos_row['source']
    row = report.value['results'].setdefault(ident, {'id': ident, 'source': source, 'status': 'running'})
    if (row.get('source') != source or ('argos' in row
            and row['argos'].get('raw_output') != argos_row.get('translated'))):
        raise AgentError('CASCADE_RESUME_CANDIDATE_MISMATCH')
    if ('argos' in row and isinstance(argos_row.get('translated'), str)
            and 'output' in row['argos']
            and row['argos']['output'] != restore_numeric_spelling(source, argos_row['translated'])[0]):
        raise AgentError('CASCADE_RESUME_CANDIDATE_MISMATCH')
    if row.get('status') in {'argos_review_accepted', 'hy7b_review_accepted', 'needs_editor'}:
        return row
    if 'argos' not in row:
        raw = argos_row.get('translated')
        stage = {'model': report.value['contract']['argos_model'], 'raw_output': raw,
                 'translation_seconds': argos_row['seconds']}
        row['argos'] = stage
        if isinstance(raw, str) and raw.strip() and not argos_row.get('error'):
            candidate, restored = restore_numeric_spelling(source, raw)
            stage.update(output=candidate, numeric_spelling_restored=restored,
                         deterministic=lexical_checks(source, candidate))
        else:
            stage['review'] = {'status': 'unverified', 'error': 'ARGOS_TRANSLATION_FAILED', 'seconds': 0}
        report.save()
    stage = row['argos']
    if 'review' not in stage:
        stage['review'] = review(report, reviewer, prompt, source, stage['output'])
        report.save()
    if (stage['review']['status'] == 'approved'
            and stage.get('deterministic', {}).get('numeric_multiset_match')):
        row.update(status='argos_review_accepted', selected_model=stage['model'], selected_output=stage['output'])
        report.save()
        return row
    row['fallback_reason'] = stage['review'].get('error') or stage['review']['status']
    if stage['review']['status'] == 'approved':
        row['fallback_reason'] = 'NUMERIC_MULTISET_UNVERIFIED'
    hy = row.get('hy7b')
    if hy is None:
        hy = row['hy7b'] = {'model': HY_MODEL, 'status': 'translation_in_progress', 'started_at': now()}
        report.save()  # Unknown translation outcomes are never silently replayed.
        started = time.monotonic()
        try:
            candidate, metrics = fallback.translate(source)
            hy.update(status='translated', output=candidate, metrics=metrics,
                      deterministic=lexical_checks(source, candidate))
        except Exception as exc:
            hy.update(status='unverified', error=safe_error(exc))
        finally:
            hy.update(translation_seconds=round(time.monotonic() - started, 6), finished_at=now())
            report.save()
    elif hy.get('status') == 'translation_in_progress':
        hy.update(status='unverified', error='TRANSLATION_OUTCOME_UNKNOWN_NO_REPLAY', finished_at=now())
        report.save()
    if hy.get('status') == 'translated':
        if 'review' not in hy:
            hy['review'] = review(report, reviewer, prompt, source, hy['output'])
        if hy['review']['status'] == 'approved' and hy['deterministic']['numeric_multiset_match']:
            row.update(status='hy7b_review_accepted', selected_model=HY_MODEL, selected_output=hy['output'])
        else:
            row.update(status='needs_editor', reason=hy['review'].get('error') or (
                hy['review']['status'] if hy['deterministic']['numeric_multiset_match']
                else 'NUMERIC_MULTISET_UNVERIFIED'))
    else:
        row.update(status='needs_editor', reason=hy.get('error', 'HY7B_TRANSLATION_UNVERIFIED'))
    report.save()
    return row


def summarize(report, order):
    controls = report.value['controls']
    critical = any(row.get('critical_false_approval') for row in controls.values())
    controls_passed = len(controls) == 2 and all(row['passed'] for row in controls.values())
    states = Counter(row['status'] for row in report.value['results'].values())
    argos_times = [row['seconds'] for row in report.value['argos_speed_samples'] if row.get('translated')]
    for row in report.value['results'].values():
        row['semantic_eligible'] = controls_passed and not critical and row['status'] in {
            'argos_review_accepted', 'hy7b_review_accepted'}
        unique_reviews = {stage['review'].get('cache_key', label): stage['review'].get('seconds', 0)
                          for label, stage in row.items()
                          if label in {'argos', 'hy7b'} and isinstance(stage, dict) and 'review' in stage}
        row['measured_component_sum_seconds'] = round(
            sum(row.get(label, {}).get('translation_seconds', 0) for label in ('argos', 'hy7b'))
            + sum(unique_reviews.values()), 6)
        row['timing_complete'] = all('seconds' in stage['review'] for label, stage in row.items()
            if label in {'argos', 'hy7b'} and isinstance(stage, dict) and 'review' in stage)
        if 'hy7b' in row and 'translation_seconds' not in row['hy7b']:
            row['timing_complete'] = False
    report.value['summary'] = {'total_unique_requested': len(order),
        'completed_unique': sum(states.get(key, 0) for key in (
            'argos_review_accepted', 'hy7b_review_accepted', 'needs_editor')),
        'states': dict(states), 'review_calls_reserved': len(report.value['reviews']),
        'unknown_interrupted_review_durations': sum('seconds' not in row
            for row in report.value['reviews'].values()),
        'review_seconds': round(sum(row.get('seconds', 0) for row in report.value['reviews'].values()), 6),
        'argos_translation_samples': len(argos_times),
        'argos_median_translation_seconds': statistics.median(argos_times) if argos_times else None,
        'hy7b_translation_seconds': round(sum(row.get('hy7b', {}).get('translation_seconds', 0)
                                            for row in report.value['results'].values()), 6),
        'new_prompt_controls_passed': controls_passed,
        'critical_false_control_approved': critical,
        'semantic_acceptance_enabled': controls_passed and not critical,
        'human_comparative_assessment_required': True}
    report.save()
    return critical


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/opt/max-postiz'))
    parser.add_argument('--argos-results', type=Path, required=True)
    parser.add_argument('--output-name', default='cascade-argos-hy7b.json')
    parser.add_argument('--hy-base-url', default='http://127.0.0.1:8094')
    parser.add_argument('--limit', type=int, help='Review at most this many unique cases; resume can increase it.')
    parser.add_argument('--controls', action='store_true')
    parser.add_argument('--full-article', action='store_true', help='Include full NASA body only, not a combined article.')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args(argv)
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}\.json', args.output_name)
            or (args.limit is not None and args.limit < 1)):
        parser.error('Use a private filename.json and a positive --limit')
    root = args.root.resolve()
    argos_path = resolve(root, args.argos_results)
    path = root / 'data/translation-benchmark' / args.output_name
    if path.resolve() == argos_path or (path.exists() and argos_path.exists() and os.path.samefile(path, argos_path)):
        parser.error('Argos input and cascade output must differ')
    corpus_path = root / 'deployment/translation/cases.json'
    prompt_path = root / 'prompts/CASCADE_REVIEW.txt'
    inputs = load_inputs(corpus_path, argos_path,
        root / 'data/article-20261018-5503.local.json' if args.full_article else None)
    prompt = prompt_path.read_text(encoding='utf-8')
    contract = {'corpus_sha256': inputs['corpus_sha256'], 'argos_sha256': inputs['argos_sha256'],
        'argos_model': inputs['packet']['model'], 'argos_engine': inputs['packet']['engine'],
        'review_model': REVIEW_MODEL, 'review_prompt_sha256': digest(prompt.encode()),
        'review_prompt_file_sha256': digest(prompt_path.read_bytes()),
        'hy_model': HY_MODEL, 'hy_base_url': args.hy_base_url,
        'full_body_included': args.full_article, 'full_body_source_sha256': inputs['full_source_sha256'],
        'controls_requested': args.controls, 'prior_prompt_approvals_comparable': False}
    report = Report(path, contract, inputs, args.resume)
    reviewer = fallback = None
    exit_code = 1
    try:
        load_env(root)
        settings = Settings.load(root)
        reviewer = OpenCode(settings)
        reviewer.model = REVIEW_MODEL
        fallback = DedicatedTranslator({'model': HY_MODEL, 'base_url': args.hy_base_url})
        if args.controls:
            run_controls(report, reviewer, prompt, inputs['corpus'])
        selected = inputs['order'][:args.limit] if args.limit else inputs['order']
        for ident in selected:
            run_case(report, reviewer, fallback, prompt, inputs['primary'][ident])
            summarize(report, inputs['order'])
        critical = summarize(report, inputs['order'])
        report.value.update(last_run_finished_at=now(),
            status='critical_reviewer_failure' if critical else
                   'complete' if report.value['summary']['completed_unique'] == len(inputs['order']) else 'partial')
        exit_code = 2 if critical else 0
    except Exception as exc:
        report.value.update(status='interrupted_or_failed', error=safe_error(exc), last_run_finished_at=now())
        if summarize(report, inputs['order']):
            exit_code = 2
    finally:
        if reviewer is not None:
            reviewer.client.close()
        if fallback is not None:
            fallback.client.close()
        report.value['exit_code'] = exit_code
        report.save()
    print(json.dumps({'status': report.value['status'], 'exit_code': exit_code,
                      'summary': report.value.get('summary'), 'report': str(path)}, ensure_ascii=False), flush=True)
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
