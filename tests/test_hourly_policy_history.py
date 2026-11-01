"""Расширение корпуса не переписывает exact политику прежних записей."""
import json
from types import SimpleNamespace

import pytest

from max_postiz_agent.article_cycle import HourlyArticleCycle
from max_postiz_agent.article_sources import policy_for_record, registered_policies
from max_postiz_agent.config import AgentError, fingerprint
from test_article_sources import URLS, configured_sources

pytest_plugins = ["test_article_sources"]


def save(settings, policy):
    directory = settings.root / 'data/hourly-policy-history'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (fingerprint(policy) + '.local.json')
    path.write_text(json.dumps(policy), encoding='utf-8')
    return path


@pytest.mark.parametrize('marker', [None, 'article-illustrated-v1', 'article-illustrated-v2'])
@pytest.mark.parametrize('source', ['nasa_svs_articles', 'noirlab_iotw', 'esahubble_potw'])
def test_exact_old_policy_restored_after_explicit_five_year_change(tmp_path, base_policy, marker, source):
    settings, _ = configured_sources()
    settings.root = tmp_path
    previous = dict(base_policy)
    if marker:
        previous.update(presentation_contract=marker, presentation_owner_instruction='Exact original image and bold instruction')
    policy = next(p for p in registered_policies(previous, settings) if p['source_id'] == source)
    current = {**base_policy, 'max_source_age_hours': 43800,
               'owner_instruction': base_policy['owner_instruction'] + ' Owner permits archive up to five years.',
               'presentation_contract': 'article-illustrated-v2',
               'presentation_owner_instruction': 'Exact original image and bold instruction'}
    record = {'source_id': source, 'url': URLS.get(source, 'https://svs.gsfc.nasa.gov/12345/'),
              'policy_hash': fingerprint(policy)}
    if marker:
        record['presentation_contract'] = marker
    path = save(settings, policy)
    before = path.read_bytes()
    assert policy_for_record(current, settings, record) == policy
    assert path.read_bytes() == before
    fresh = {k: v for k, v in record.items() if k != 'policy_hash'}
    assert policy_for_record(current, settings, fresh)['max_source_age_hours'] == 43800


@pytest.mark.parametrize('change', ['tamper', 'duplicate_key', 'symlink', 'wrong_target', 'wrong_rights', 'wrong_marker', 'oversize'])
def test_archive_cannot_replace_pinned_permissions_or_presentation(tmp_path, base_policy, change):
    settings, _ = configured_sources()
    settings.root = tmp_path
    old = dict(base_policy)
    current = {**base_policy, 'max_source_age_hours': 43800}
    if change == 'wrong_target':
        old['target_id'] = 'other'
    if change == 'wrong_rights':
        old['rights_evidence'] = 'https://untrusted.invalid/'
    if change == 'wrong_marker':
        old.update(presentation_contract='article-illustrated-v1', presentation_owner_instruction='Original owner quote')
    path = save(settings, old)
    record = {'source_id': 'nasa_svs_articles', 'url': 'https://svs.gsfc.nasa.gov/12345/', 'policy_hash': fingerprint(old)}
    if change == 'tamper':
        path.write_text('{}')
    if change == 'duplicate_key':
        path.write_text('{"enabled":true,"enabled":false}')
    if change == 'oversize':
        path.write_bytes(b'x' * (64 * 1024 + 1))
    if change == 'symlink':
        other = tmp_path / 'other.json'
        other.write_bytes(path.read_bytes())
        path.unlink()
        try:
            path.symlink_to(other)
        except OSError:
            pytest.skip('Symlink privilege unavailable on this host')
    with pytest.raises(AgentError, match='ARTICLE_POLICY_HISTORY_INVALID'):
        policy_for_record(current, settings, record)


@pytest.mark.parametrize('age', [72, 8760, 8761, 43800])
def test_native_policy_accepts_explicit_age_up_to_five_years(tmp_path, base_policy, age):
    path = tmp_path / 'data/hourly-article-policy.local.json'
    path.parent.mkdir()
    path.write_text(json.dumps({**base_policy, 'max_source_age_hours': age}))
    cycle = object.__new__(HourlyArticleCycle)
    cycle.core = SimpleNamespace(settings=SimpleNamespace(root=tmp_path))
    assert cycle._policy()['max_source_age_hours'] == age


@pytest.mark.parametrize('age', [True, 71, 43801, 43800.0, '43800'])
def test_native_policy_still_rejects_unbounded_or_invalid_age(tmp_path, base_policy, age):
    path = tmp_path / 'data/hourly-article-policy.local.json'
    path.parent.mkdir()
    path.write_text(json.dumps({**base_policy, 'max_source_age_hours': age}))
    cycle = object.__new__(HourlyArticleCycle)
    cycle.core = SimpleNamespace(settings=SimpleNamespace(root=tmp_path))
    with pytest.raises(AgentError, match='ARTICLE_POLICY_INVALID'):
        cycle._policy()
