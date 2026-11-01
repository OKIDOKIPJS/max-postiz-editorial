"""Documented native text label is distinct from approved editorial tags."""
from copy import deepcopy

import pytest
from bs4 import BeautifulSoup

from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.pikabu_delivery import (
    READBACK_CONTRACT, TAG_POLICY, TAG_POLICY_SOURCE, parse_public_story, public_tag_contract,
)
from test_pikabu_delivery import _story_html, post as post


def public_html(snapshot, actual_tags, *, hidden=None):
    soup = BeautifulSoup(_story_html("https://pikabu.ru", snapshot), "html.parser")
    tags = soup.select_one("article.story .tags")
    tags.clear()
    for value in actual_tags:
        tag = soup.new_tag("a", attrs={"class": "tags__tag"})
        tag.string = value
        if value == hidden:
            tag["hidden"] = ""
        tags.append(tag)
    return str(soup).encode("utf-8")


@pytest.mark.parametrize("added", [[], ["Текст"]])
def test_exact_declared_or_one_native_text_label_has_separate_bound_proof(post, added):
    post["media"] = []
    original = deepcopy(post)
    declared = post["metadata"]["tags"]
    proof = parse_public_story(public_html(post, list(reversed(declared + added))),
        "https://pikabu.ru/story/test_123", post, "fixture_owner", encoding="utf-8")
    assert proof["contract"] == READBACK_CONTRACT
    assert proof["tag_policy"] == TAG_POLICY and proof["tag_policy_source"] == TAG_POLICY_SOURCE
    assert proof["platform_added_tags"] == added
    assert proof["declared_tags_sha256"] == proof["tags_sha256"] == fingerprint(sorted(t.casefold() for t in declared))
    assert proof["effective_tags_sha256"] == fingerprint(sorted(t.casefold() for t in declared + added))
    assert post == original


@pytest.mark.parametrize("damage", ["missing", "duplicate_native", "duplicate_declared", "other_extra", "hidden_extra", "longpost"])
def test_native_rule_never_accepts_missing_duplicate_or_other_public_tag(post, damage):
    post["media"] = []
    actual = post["metadata"]["tags"] + ["Текст"]
    hidden = None
    if damage == "missing":
        actual.pop(0)
    elif damage == "duplicate_native":
        actual.append("Текст")
    elif damage == "duplicate_declared":
        actual.append(actual[0])
    else:
        actual.append("Длиннопост" if damage == "longpost" else "Несогласованный тег")
        if damage == "hidden_extra":
            hidden = actual[-1]
    with pytest.raises(AgentError, match="PIKABU_READBACK_TAGS_MISMATCH"):
        parse_public_story(public_html(post, actual, hidden=hidden),
            "https://pikabu.ru/story/test_123", post, "fixture_owner", encoding="utf-8")


def test_native_text_label_never_exempts_image_post_or_missing_media_binding(post):
    actual = post["metadata"]["tags"] + ["Текст"]
    for value in (deepcopy(post), {key: item for key, item in post.items() if key != "media"}):
        with pytest.raises(AgentError, match="PIKABU_READBACK_TAGS_MISMATCH"):
            parse_public_story(public_html(value, actual), "https://pikabu.ru/story/test_123",
                               value, "fixture_owner", encoding="utf-8")


def test_declared_text_is_not_reclassified_or_allowed_twice(post):
    post["media"] = []
    post["metadata"]["tags"].append("Текст")
    proof = parse_public_story(public_html(post, post["metadata"]["tags"]),
        "https://pikabu.ru/story/test_123", post, "fixture_owner", encoding="utf-8")
    assert proof["platform_added_tags"] == []
    with pytest.raises(AgentError, match="PIKABU_READBACK_TAGS_MISMATCH"):
        parse_public_story(public_html(post, post["metadata"]["tags"] + ["Текст"]),
            "https://pikabu.ru/story/test_123", post, "fixture_owner", encoding="utf-8")


@pytest.mark.parametrize("added", [None, {}, "Текст", ["текст"], ["Текст", "Текст"], ["Политика"]])
def test_proof_native_tag_list_is_typed_canonical_and_not_an_allowlist(post, added):
    post["media"] = []
    with pytest.raises(AgentError, match="PIKABU_READBACK_TAGS_MISMATCH"):
        public_tag_contract(post, added)
