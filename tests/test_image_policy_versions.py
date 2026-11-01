"""Активация картинок не переписывает hashes прежних exact согласований."""
import copy
import json
from types import SimpleNamespace

import pytest

from max_postiz_agent.article_sources import policy_for_article, policy_for_record, registered_policies
from max_postiz_agent.article_attribution import ILLUSTRATED_PRESENTATION_CONTRACTS
from max_postiz_agent.article_cycle import HourlyArticleCycle
from max_postiz_agent.config import AgentError, fingerprint
from test_article_sources import URLS, configured_sources

pytest_plugins = ["test_article_sources"]


@pytest.mark.parametrize('source_id', ['nasa_svs_articles', 'noirlab_iotw', 'esahubble_potw'])
@pytest.mark.parametrize('contract', sorted(ILLUSTRATED_PRESENTATION_CONTRACTS))
def test_previous_policy_hashes_survive_presentation_activation(base_policy, source_id, contract):
    settings, _ = configured_sources()
    previous = next(p for p in registered_policies(base_policy, settings) if p['source_id'] == source_id)
    current = {**copy.deepcopy(base_policy), 'presentation_contract': contract,
               'presentation_owner_instruction': 'Owner explicitly requests illustrations and neutral credit'}
    url = URLS.get(source_id, 'https://svs.gsfc.nasa.gov/12345/')
    old_record = {'source_id': source_id, 'url': url, 'policy_hash': fingerprint(previous)}
    assert policy_for_record(current, settings, old_record) == previous
    assert fingerprint(policy_for_record(current, settings, old_record)) == old_record['policy_hash']
    selected = policy_for_article(current, settings, {'url': url})
    assert selected['presentation_contract'] == contract
    assert selected['presentation_owner_instruction'] == current['presentation_owner_instruction']
    assert fingerprint(selected) != fingerprint(previous)
    new_record = {**old_record, 'presentation_contract': contract, 'policy_hash': fingerprint(selected)}
    assert policy_for_record(current, settings, new_record) == selected
    assert 'presentation_contract' not in previous


def test_unknown_presentation_marker_cannot_use_legacy_policy(base_policy):
    settings, _ = configured_sources()
    with pytest.raises(AgentError, match='ARTICLE_PRESENTATION_CONTRACT_INVALID'):
        policy_for_record(base_policy, settings, {'source_id': 'noirlab_iotw',
            'url': URLS['noirlab_iotw'], 'presentation_contract': 'arbitrary-images'})


@pytest.mark.parametrize('source_id', ['nasa_svs_articles', 'noirlab_iotw', 'esahubble_potw'])
def test_v2_restores_exact_previous_v1_policy_without_rewriting_owner_quote(base_policy, source_id):
    settings, _ = configured_sources()
    old_base = {**base_policy, 'presentation_contract': 'article-illustrated-v1',
                'presentation_owner_instruction': 'Exact owner image and bold instruction, retained verbatim'}
    new_base = {**old_base, 'presentation_contract': 'article-illustrated-v2'}
    old = next(p for p in registered_policies(old_base, settings) if p['source_id'] == source_id)
    url = URLS.get(source_id, 'https://svs.gsfc.nasa.gov/12345/')
    record = {'source_id': source_id, 'url': url, 'presentation_contract': 'article-illustrated-v1',
              'policy_hash': fingerprint(old)}
    restored = policy_for_record(new_base, settings, record)
    assert restored == old
    assert fingerprint(restored) == record['policy_hash']
    current = policy_for_article(new_base, settings, {'url': url})
    assert current['presentation_contract'] == 'article-illustrated-v2'
    assert fingerprint(current) != record['policy_hash']
    assert old_base['presentation_owner_instruction'] == new_base['presentation_owner_instruction']


def test_previous_v1_reserve_is_not_ready_for_future_v2_policy():
    from max_postiz_agent.article_reserve import ready_for_policy
    record = {'state': 'quality_ready', 'presentation_contract': 'article-illustrated-v1'}
    assert not ready_for_policy(record, {'presentation_contract': 'article-illustrated-v2'})


@pytest.mark.parametrize('contract', sorted(ILLUSTRATED_PRESENTATION_CONTRACTS))
def test_cycle_policy_parser_preserves_exact_explicit_presentation_version(tmp_path, base_policy, contract):
    directory = tmp_path / 'data'
    directory.mkdir()
    current = {**base_policy, 'presentation_contract': contract,
        'presentation_owner_instruction': 'Owner requires licensed images and a bold title'}
    path = directory / 'hourly-article-policy.local.json'
    raw = json.dumps(current, ensure_ascii=False).encode('utf-8')
    path.write_bytes(raw)
    cycle = object.__new__(HourlyArticleCycle)
    cycle.core = SimpleNamespace(settings=SimpleNamespace(root=tmp_path))
    assert cycle._policy() == current
    assert path.read_bytes() == raw


@pytest.mark.parametrize('contract', [False, {}, [], 'article-illustrated-v3', 'article-illustrated-v2 '])
def test_cycle_policy_parser_never_treats_unknown_presentation_as_legacy(tmp_path, base_policy, contract):
    directory = tmp_path / 'data'
    directory.mkdir()
    current = {**base_policy, 'presentation_contract': contract,
        'presentation_owner_instruction': 'Owner requires licensed images and a bold title'}
    (directory / 'hourly-article-policy.local.json').write_text(json.dumps(current), encoding='utf-8')
    cycle = object.__new__(HourlyArticleCycle)
    cycle.core = SimpleNamespace(settings=SimpleNamespace(root=tmp_path))
    with pytest.raises(AgentError, match='ARTICLE_POLICY_INVALID'):
        cycle._policy()
