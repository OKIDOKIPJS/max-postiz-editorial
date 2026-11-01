"""Лицензированное изображение проверяется по полному сохранённому source proof."""
import copy
import hashlib
import io
import json
import re
import threading
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from max_postiz_agent.article_attribution import CC_BY_URL
from max_postiz_agent.article_images import (
    CONTRACT, HOSTS, MAX_BYTES, MAX_PARTS, PART_BYTES, _rights_contact_hash,
    image_candidate, prepare_article_image, validate_article_image,
)
from max_postiz_agent.config import AgentError, fingerprint

pytest_plugins = ["test_server_science"]


def jpeg(size=(1600, 900), color="navy", format="JPEG"):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format=format)
    return output.getvalue()


@pytest.fixture(params=["article-illustrated-v1", "article-illustrated-v2"])
def illustration(source, request):
    host, prefix = HOSTS[source.source_id]
    image_url = "https://" + host + prefix + "publicationjpg/" + source.ident + ".jpg"
    page = source.pages[source.path].replace("</html>",
        '<a href="' + image_url + '">Publication JPEG</a></html>')
    source.pages[source.path] = page
    article = source.run()
    source.core.settings.app = {"media_dir": "data/media"}
    contract = request.param
    policy = {**source.policy, "presentation_contract": contract}
    record = {"id": "image-record-1", "source_id": source.source_id, "url": article["url"],
              "input_hash": fingerprint(article), "policy_hash": fingerprint(policy),
              "presentation_contract": contract}
    proofs = list((source.root / "data/server-sources").glob(source.source_id + "-*.local.json"))
    assert len(proofs) == 1
    return SimpleNamespace(source=source, core=source.core, article=article, policy=policy,
        record=record, root=source.root, page=page, image_url=image_url, source_path=proofs[0])


def download(value, raw=None, status=200, mime="image/jpeg", fail=None):
    calls = []
    raw = jpeg() if raw is None else raw

    def get(request):
        calls.append(request)
        assert request.method == "GET" and str(request.url) == value.image_url
        if fail:
            raise fail("isolated read failure", request=request)
        return httpx.Response(status, content=raw, headers={"content-type": mime,
            "location": "https://evil.invalid/retry.jpg"})

    return httpx.MockTransport(get), calls, raw


def prepare(value):
    transport, calls, raw = download(value)
    proof = prepare_article_image(value.core, value.article, value.record, value.policy, transport=transport)
    value.record.update(illustration=proof, image_credit=proof["credit"], illustration_proof_hash=fingerprint(proof))
    assert len(calls) == 1
    return proof, raw


def rewrite_proof(value, proof):
    path = value.root / "data/article-images/image-record-1.proof.local.json"
    path.write_text(json.dumps(proof, ensure_ascii=False), encoding="utf-8")
    value.record.update(illustration=proof, image_credit=proof["credit"], illustration_proof_hash=fingerprint(proof))


def cf_contact(key, email=b"contact@example.test", style="double"):
    encoded = bytes([key]) + bytes(char ^ key for char in email)
    href = "/cdn-cgi/l/email-protection#" + encoded.hex()
    quote = {"double": '"', "single": "'", "bare": ""}.get(style, '"')
    result = (f'<a href={quote}{href}{quote} title="Public contact">'
        f'<span class="__cf_email__" data-cfemail={quote}{encoded.hex()}{quote}>'
        '[email&#160;protected]</span></a>')
    if style == "uppercase":
        result = result.replace(encoded.hex(), encoded.hex().upper()).replace("href=", "HREF=")
    if style == "multiline":
        result = "\r\n" + result.replace(" data-cfemail=", "\r\n\tdata-cfemail=")
    return result


@pytest.mark.parametrize("style", ["double", "single", "bare", "uppercase", "multiline"])
def test_only_cloudflare_key_representation_is_equivalent_with_exact_offsets(style):
    before, after = ("<!--keep-->" + cf_contact(key, style=style) + "\n<p>CC BY 4.0</p>" for key in (17, 93))
    assert before != after and _rights_contact_hash(before) == _rights_contact_hash(after)
    assert _rights_contact_hash(before) != hashlib.sha256(before.encode()).hexdigest()


@pytest.mark.parametrize("change", ["email", "terms", "license", "other_href", "other_attribute", "space", "quote", "comment"])
def test_contact_equivalence_preserves_every_other_literal_html_character(change):
    before = cf_contact(17) + '<p>CC BY 4.0</p><a href="https://example.test/license">License</a>'
    after = cf_contact(93) + '<p>CC BY 4.0</p><a href="https://example.test/license">License</a>'
    if change == "email":
        after = after.replace(cf_contact(93), cf_contact(93, b"other@example.test"))
    elif change == "terms":
        after = after.replace("CC BY 4.0", "CC BY-NC 4.0")
    elif change == "license":
        after = after.replace("/license", "/other-license")
    elif change == "other_href":
        after += '<a href="https://other.test/">Other</a>'
    elif change == "other_attribute":
        after = after.replace("Public contact", "Different contact label")
    elif change == "space":
        after = after.replace("<p>", "<p >")
    elif change == "quote":
        after = after.replace('title="Public contact"', "title='Public contact'")
    else:
        after += "<!--different-->"
    assert _rights_contact_hash(before) != _rights_contact_hash(after)


@pytest.mark.parametrize("payload", ["", "01", "010", "not-hex", "00ff4041", "006140620a", "00616263", "00" * 322])
def test_invalid_cloudflare_contact_payload_fails_closed(payload):
    with pytest.raises(AgentError, match="ARTICLE_IMAGE_PROOF_INVALID"):
        _rights_contact_hash('<span data-cfemail="' + payload + '">Contact</span>')


@pytest.mark.parametrize("html", ["<span data-cfemail>Contact</span>",
    '<span data-cfemail="00614062" data-cfemail="00614062">Contact</span>'])
def test_missing_or_duplicate_contact_attributes_are_not_normalized(html):
    with pytest.raises(AgentError, match="ARTICLE_IMAGE_PROOF_INVALID"):
        _rights_contact_hash(html)


def test_normalization_does_not_match_attribute_names_inside_comments_scripts_or_other_values():
    contact = cf_contact(17)
    html = ('<!--' + contact + '--><script>const html = ' + json.dumps(contact) + ';</script>'
        '<p title="data-cfemail=00614062">Plain text</p><div data-cfemail="00614062">Other tag</div>')
    assert _rights_contact_hash(html) == hashlib.sha256(html.encode()).hexdigest()


def duplicate_cf_source_proofs(value, change=None):
    first = json.loads(value.source_path.read_text())
    first["rights_html"] = first["rights_html"].replace("</html>", cf_contact(17) + "</html>")
    second = copy.deepcopy(first)
    second["rights_html"] = second["rights_html"].replace(cf_contact(17), cf_contact(93))
    if change == "email":
        second["rights_html"] = second["rights_html"].replace(cf_contact(93), cf_contact(93, b"other@example.test"))
    elif change == "terms":
        second["rights_html"] = second["rights_html"].replace("Attribution 4.0", "Attribution-NonCommercial 4.0")
    elif change == "other_href":
        second["rights_html"] = second["rights_html"].replace("</html>", '<a href="https://other.test/">Other</a></html>')
    elif change == "other_html":
        second["rights_html"] = second["rights_html"].replace("</html>", "<!--different--></html>")
    elif change == "license":
        second["rights_html"] = second["rights_html"].replace('href="http://creativecommons.org/', 'href="https://creativecommons.org/')
    elif change == "page":
        second["page_html"] = second["page_html"].replace("</html>", "<!--different--></html>")
    elif change == "image":
        second["page_html"] = second["page_html"].replace(value.image_url, value.image_url.replace(".jpg", "-other.jpg"))
    elif change == "credit":
        second["page_html"] = second["page_html"].replace("Jane Scientist", "Other Owner")
        second["credit"] = second["credit"].replace("Jane Scientist", "Other Owner")
    elif change == "copyright":
        second["page_html"] = second["page_html"].replace("Jane Scientist", "Jane Scientist, all rights reserved")
        second["credit"] += ", all rights reserved"
    for proof in (first, second):
        for key in ("rights", "page"):
            proof["hashes"][key] = hashlib.sha256(proof[key + "_html"].encode()).hexdigest()
    if change == "raw_hash":
        second["hashes"]["rights"] = "0" * 64
    other = value.source_path.with_name(value.source.source_id + "-zz-cf-contact.local.json")
    value.source_path.write_text(json.dumps(first), encoding="utf-8")
    other.write_text(json.dumps(second), encoding="utf-8")
    return first, other


def test_duplicate_contact_obfuscation_keeps_raw_proofs_hashes_selection_and_one_download(illustration):
    value = illustration
    first, other = duplicate_cf_source_proofs(value)
    raw = {path: path.read_bytes() for path in (value.source_path, other)}
    proof, _ = prepare(value)
    assert proof["source_proof"] == value.source_path.relative_to(value.root).as_posix()
    assert proof["source_proof_sha256"] == hashlib.sha256(raw[value.source_path]).hexdigest()
    assert proof["rights_sha256"] == first["hashes"]["rights"]
    assert validate_article_image(value.core, value.record) == [proof["media"]]
    assert all(path.read_bytes() == data for path, data in raw.items())


@pytest.mark.parametrize("change", ["email", "terms", "other_href", "other_html", "license", "raw_hash",
                                    "page", "image", "credit", "copyright"])
def test_contact_equivalence_never_accepts_other_rights_page_image_or_credit_changes(illustration, change):
    value = illustration
    duplicate_cf_source_proofs(value, change)
    with pytest.raises(AgentError):
        prepare_article_image(value.core, value.article, value.record, value.policy,
            transport=httpx.MockTransport(lambda _: pytest.fail("different proof reached image download")))
    assert not (value.root / "data/article-images/image-record-1.claim.local.json").exists()


def test_official_page_exact_download_credit_and_rights_yield_canonical_good_jpeg_once(illustration):
    value = illustration
    proof, raw = prepare(value)
    media = proof["media"]
    assert media == {"path": "data/media/" + hashlib.sha256(raw).hexdigest() + ".jpg",
                     "sha256": hashlib.sha256(raw).hexdigest(), "mime": "image/jpeg"}
    assert (value.root / media["path"]).read_bytes() == raw
    assert proof["width"] == 1600 and proof["height"] == 900 and proof["bytes"] == len(raw)
    assert proof["license"] == CC_BY_URL and proof["image_url"] == value.image_url
    assert proof["contract"] == value.record["presentation_contract"]
    assert proof["credit"] == value.source.adapter["publisher"] + "/Jane Scientist"
    claim = json.loads((value.root / "data/article-images/image-record-1.claim.local.json").read_text())
    assert claim["input"]["contract"] == proof["contract"]
    assert "presentation_contract" not in proof and "presentation_contract" not in claim["input"]
    assert claim["automatic_retry"] is False and claim["input"]["input_hash"] == fingerprint(value.article)
    assert validate_article_image(value.core, value.record) == [media]
    never = httpx.MockTransport(lambda _: pytest.fail("verified image must not be downloaded twice"))
    assert prepare_article_image(value.core, value.article, value.record, value.policy, transport=never) == proof


@pytest.mark.parametrize("change", ["record", "proof", "variant", "replay_policy"])
def test_other_valid_presentation_contract_cannot_rebind_existing_image_proof(illustration, change):
    value = illustration
    proof, _ = prepare(value)
    other = "article-illustrated-v2" if proof["contract"] == "article-illustrated-v1" else "article-illustrated-v1"
    variant = SimpleNamespace(body="Checked caption", media=[copy.deepcopy(proof["media"])],
        metadata_json=copy.deepcopy(value.record))
    if change == "record":
        value.record["presentation_contract"] = other
    elif change == "variant":
        variant.metadata_json["presentation_contract"] = other
    elif change == "proof":
        proof = {**proof, "contract": other}
        rewrite_proof(value, proof)
        claim_path = value.root / "data/article-images/image-record-1.claim.local.json"
        claim = json.loads(claim_path.read_text())
        claim["input"]["contract"] = other
        claim_path.write_text(json.dumps(claim), encoding="utf-8")
    else:
        value.policy["presentation_contract"] = other
        value.record["presentation_contract"] = other
        value.record["policy_hash"] = fingerprint(value.policy)
        with pytest.raises(AgentError, match="ARTICLE_IMAGE_PROOF_INVALID"):
            prepare_article_image(value.core, value.article, value.record, value.policy,
                transport=httpx.MockTransport(lambda _: pytest.fail("cross-contract replay attempted GET")))
        return
    with pytest.raises(AgentError, match="ARTICLE_IMAGE_PROOF_INVALID"):
        validate_article_image(value.core, value.record, variant)


@pytest.mark.parametrize("marker", ["article-illustrated-v3", "unknown", [], {}, True])
def test_unknown_or_non_string_presentation_blocks_before_image_http(illustration, marker):
    value = illustration
    value.record["presentation_contract"] = marker
    value.policy["presentation_contract"] = marker
    value.record["policy_hash"] = fingerprint(value.policy)
    with pytest.raises(AgentError, match="ARTICLE_IMAGE_PROOF_INVALID"):
        prepare_article_image(value.core, value.article, value.record, value.policy,
            transport=httpx.MockTransport(lambda _: pytest.fail("invalid contract attempted HTTP")))


def test_durable_proof_recovers_known_completed_download_without_new_get(illustration):
    value = illustration
    transport, calls, _ = download(value)
    proof = prepare_article_image(value.core, value.article, value.record, value.policy, transport=transport)
    assert value.record.get("illustration") is None
    assert prepare_article_image(value.core, value.article, value.record, value.policy,
        transport=httpx.MockTransport(lambda _: pytest.fail("saved proof must be replayed locally"))) == proof
    assert len(calls) == 1


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
def test_failed_or_redirected_get_is_not_retried_and_retains_claim(illustration, status):
    value = illustration
    transport, calls, _ = download(value, status=status)
    code = "ARTICLE_IMAGE_SOURCE_BLOCKED" if status in {401, 403, 429} else "ARTICLE_IMAGE_SOURCE_UNAVAILABLE"
    with pytest.raises(AgentError, match=code):
        prepare_article_image(value.core, value.article, value.record, value.policy, transport=transport)
    with pytest.raises(AgentError, match="ARTICLE_IMAGE_DOWNLOAD_RECONCILIATION_REQUIRED"):
        prepare_article_image(value.core, value.article, value.record, value.policy, transport=transport)
    assert len(calls) == 1
    assert (value.root / "data/article-images/image-record-1.claim.local.json").is_file()
    assert not (value.root / "data/article-images/image-record-1.proof.local.json").exists()


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError])
def test_unknown_download_outcome_never_gets_an_automatic_second_request(illustration, failure):
    value = illustration
    transport, calls, _ = download(value, fail=failure)
    with pytest.raises(AgentError, match="ARTICLE_IMAGE_SOURCE_UNAVAILABLE"):
        prepare_article_image(value.core, value.article, value.record, value.policy, transport=transport)
    with pytest.raises(AgentError, match="ARTICLE_IMAGE_DOWNLOAD_RECONCILIATION_REQUIRED"):
        prepare_article_image(value.core, value.article, value.record, value.policy, transport=transport)
    assert len(calls) == 1
    assert not (value.root / "data/article-images/image-record-1.part").exists()


@pytest.mark.parametrize("kind", ["small", "narrow", "png", "wrong_mime", "invalid", "oversize"])
def test_image_quality_format_and_size_fail_without_text_fallback_or_retry(illustration, kind):
    value = illustration
    raw = {"small": lambda: jpeg((800, 600)), "narrow": lambda: jpeg((1600, 799)),
           "png": lambda: jpeg(format="PNG"), "wrong_mime": jpeg,
           "invalid": lambda: b"not an image", "oversize": lambda: b"x" * (MAX_BYTES + 1)}[kind]()
    transport, calls, _ = download(value, raw=raw, mime="image/png" if kind == "wrong_mime" else "image/jpeg")
    with pytest.raises(AgentError):
        prepare_article_image(value.core, value.article, value.record, value.policy, transport=transport)
    with pytest.raises(AgentError, match="ARTICLE_IMAGE_DOWNLOAD_RECONCILIATION_REQUIRED"):
        prepare_article_image(value.core, value.article, value.record, value.policy, transport=transport)
    assert len(calls) == 1 and value.record.get("illustration") is None


@pytest.mark.parametrize("change", [
    "wrong_host", "wrong_id", "other_format", "missing_download", "multiple_downloads",
    "missing_credit", "duplicate_credit", "copyright", "no_derivatives", "portrait",
])
def test_page_candidate_never_widens_credit_or_exact_official_image_url(illustration, change):
    value = illustration
    page = value.page
    if change == "wrong_host":
        page = page.replace(value.image_url, value.image_url.replace("https://", "https://evil.invalid/"))
    elif change == "wrong_id":
        page = page.replace(value.image_url, value.image_url.replace(value.source.ident, "other1234"))
    elif change == "other_format":
        page = page.replace("publicationjpg/", "large/")
    elif change == "missing_download":
        page = page.replace("Publication JPEG", "Original TIFF")
    elif change == "multiple_downloads":
        page += '<a href="https://evil.invalid/second.jpg">Publication JPEG</a>'
    elif change == "missing_credit":
        page = page.replace('class="credit"', 'class="other"')
    elif change == "duplicate_credit":
        page = page.replace('<div class="credit">', '<div class="credit">Duplicate</div><div class="credit">')
    elif change == "copyright":
        page = page.replace("Jane Scientist", "Jane Scientist, all rights reserved")
    elif change == "no_derivatives":
        page = page.replace("Jane Scientist", "Jane Scientist, CC BY-ND")
    else:
        page = page.replace("Jane Scientist", "Jane Scientist, group photograph of students")
    with pytest.raises(AgentError):
        image_candidate(value.source.source_id, value.article["url"], page)


def test_credit_links_are_retained_in_exact_attribution_without_being_downloaded(illustration):
    value = illustration
    page = value.page.replace("Jane Scientist", '<a href="https://photographer.example/profile/">Jane Scientist</a>')
    result = image_candidate(value.source.source_id, value.article["url"], page)
    assert result["credit"].endswith(" — https://photographer.example/profile/")
    assert result["credit_links"] == ["https://photographer.example/profile/"]


@pytest.mark.parametrize("change", ["policy", "input", "credit", "license", "image_url", "width", "bytes", "mime",
                                   "extra_media_key", "file_bytes", "source_page", "source_rights", "source_credit"])
def test_rebound_hash_cannot_hide_tampered_source_rights_credit_or_image(illustration, change):
    value = illustration
    proof, _ = prepare(value)
    proof = copy.deepcopy(proof)
    if change == "policy":
        value.record["policy_hash"] = "f" * 64
    elif change == "input":
        value.record["input_hash"] = "f" * 64
    elif change == "credit":
        proof["credit"] = "Different copyright owner"
    elif change == "license":
        proof["license"] = "https://creativecommons.org/licenses/by-nc/4.0/"
    elif change == "image_url":
        proof["image_url"] = "https://evil.invalid/photo.jpg"
    elif change in {"width", "bytes"}:
        proof[change] += 1
    elif change == "mime":
        proof["media"]["mime"] = "image/png"
    elif change == "extra_media_key":
        proof["media"]["unbound"] = True
    elif change == "file_bytes":
        (value.root / proof["media"]["path"]).write_bytes(jpeg(color="red"))
    else:
        source = json.loads(value.source_path.read_text())
        if change == "source_page":
            source["page_html"] = source["page_html"].replace("young stars", "old stars")
            source["hashes"]["page"] = hashlib.sha256(source["page_html"].encode()).hexdigest()
            proof["page_sha256"] = source["hashes"]["page"]
        elif change == "source_rights":
            source["rights_html"] = source["rights_html"].replace("Attribution 4.0", "Attribution-NonCommercial 4.0")
            source["hashes"]["rights"] = hashlib.sha256(source["rights_html"].encode()).hexdigest()
            proof["rights_sha256"] = source["hashes"]["rights"]
        else:
            source["page_html"] = source["page_html"].replace("Jane Scientist", "Other Owner")
            source["credit"] = source["credit"].replace("Jane Scientist", "Other Owner")
            source["hashes"]["page"] = hashlib.sha256(source["page_html"].encode()).hexdigest()
            proof["page_sha256"] = source["hashes"]["page"]
        value.source_path.write_text(json.dumps(source), encoding="utf-8")
        proof["source_proof_sha256"] = hashlib.sha256(value.source_path.read_bytes()).hexdigest()
    rewrite_proof(value, proof)
    with pytest.raises(AgentError):
        validate_article_image(value.core, value.record)


@pytest.mark.parametrize("change", ["media", "metadata", "marker", "credit"])
def test_variant_must_have_exact_same_media_proof_and_credit(illustration, change):
    value = illustration
    proof, _ = prepare(value)
    variant = SimpleNamespace(body="Checked article caption", media=[copy.deepcopy(proof["media"])], metadata_json=copy.deepcopy(value.record))
    assert validate_article_image(value.core, value.record, variant) == [proof["media"]]
    if change == "media":
        variant.media = []
    elif change == "metadata":
        variant.metadata_json["illustration"] = {}
    elif change == "marker":
        variant.metadata_json["presentation_contract"] = None
    else:
        variant.metadata_json["image_credit"] = "Other owner"
    with pytest.raises(AgentError):
        validate_article_image(value.core, value.record, variant)


def test_unsupported_nasa_does_not_fall_back_to_any_remote_image_or_text_only(tmp_path):
    core = SimpleNamespace(settings=SimpleNamespace(root=tmp_path, app={"media_dir": "data/media"}))
    article = {"url": "https://svs.gsfc.nasa.gov/5682/", "title": "A scientific title", "original": "Source", "published_at": 1}
    policy = {"presentation_contract": CONTRACT, "rights_evidence": "https://www.nasa.gov/"}
    record = {"id": "nasa-record", "presentation_contract": CONTRACT, "source_id": "nasa_svs_articles"}
    with pytest.raises(AgentError):
        image_candidate(record["source_id"], article["url"], "")
    with pytest.raises(AgentError):
        prepare_article_image(core, article, record, policy,
            transport=httpx.MockTransport(lambda _: pytest.fail("unsupported source must never reach HTTP")))


def test_legacy_text_record_never_silently_accepts_an_image(tmp_path):
    core = SimpleNamespace(settings=SimpleNamespace(root=tmp_path))
    assert validate_article_image(core, {}) == []
    with pytest.raises(AgentError, match="ARTICLE_MEDIA_NOT_ALLOWED"):
        validate_article_image(core, {}, SimpleNamespace(media=[{"path": "anything.jpg"}], metadata_json={}))


def ranged_download(value, *, mutate=None, barrier=False):
    raw = jpeg()
    etag = '"fixed-image-version"'
    calls, active = [], {"now": 0, "maximum": 0}
    lock = threading.Lock()
    rendezvous = threading.Barrier(2) if barrier else None

    def get(request):
        assert request.method == "GET" and str(request.url) == value.image_url
        assert request.headers["accept-encoding"] == "identity"
        match = re.fullmatch(r"bytes=(\d+)-(\d+)", request.headers["range"])
        assert match
        start, stop = map(int, match.groups())
        assert request.headers.get("if-match") == (None if start == 0 else etag)
        claim = value.root / ("data/article-images/image-record-1.claim.local.json.part-" + str(start) + ".local.json")
        assert json.loads(claim.read_text()) == {"start": start, "stop": stop,
            "etag": None if start == 0 else etag, "automatic_retry": False}
        with lock:
            calls.append(start)
            active["now"] += 1
            active["maximum"] = max(active["maximum"], active["now"])
        try:
            if rendezvous is not None and start in {PART_BYTES, PART_BYTES * 2}:
                rendezvous.wait(timeout=5)
            last = min(stop, len(raw) - 1)
            headers = {"content-type": "image/jpeg", "content-range": f"bytes {start}-{last}/{len(raw)}", "etag": etag}
            status, content = 206, raw[start:last + 1]
            if mutate:
                status, headers, content = mutate(start, stop, status, headers, content, raw)
            return httpx.Response(status, headers=headers, content=content)
        finally:
            with lock:
                active["now"] -= 1

    return httpx.MockTransport(get), calls, raw, active


def test_ranges_bind_if_match_each_durable_part_and_exact_full_image(illustration):
    value = illustration
    transport, calls, raw, active = ranged_download(value, barrier=True)
    proof = prepare_article_image(value.core, value.article, value.record, value.policy, transport=transport)
    value.record.update(illustration=proof, image_credit=proof["credit"], illustration_proof_hash=fingerprint(proof))
    assert (value.root / proof["media"]["path"]).read_bytes() == raw
    assert sorted(calls) == list(range(0, len(raw), PART_BYTES))
    assert len(calls) == len(set(calls)) and active["maximum"] == 2
    expected = [{"start": at, "bytes": len(raw[at:at + PART_BYTES]),
                 "sha256": hashlib.sha256(raw[at:at + PART_BYTES]).hexdigest()}
                for at in range(0, len(raw), PART_BYTES)]
    assert proof["download"] == {"contract": "image-http-ranges-v1", "etag": '"fixed-image-version"',
                                 "bytes": len(raw), "parts": expected}
    assert validate_article_image(value.core, value.record) == [proof["media"]]


def test_ranged_budget_covers_the_same_ten_mib_as_full_download():
    assert PART_BYTES * MAX_PARTS >= MAX_BYTES
    assert PART_BYTES * (MAX_PARTS - 1) < MAX_BYTES


def test_ranged_download_above_previous_four_mib_limit_retains_every_claim(tmp_path):
    from max_postiz_agent.article_images import _download
    raw = b'x' * (4 * 1024 * 1024 + 1)
    claim = tmp_path / 'download.claim.local.json'
    claim.write_text('{}')
    calls = []

    def get(request):
        start, stop = map(int, request.headers['range'].removeprefix('bytes=').split('-'))
        assert start not in calls
        calls.append(start)
        assert request.headers.get('if-match') == (None if start == 0 else '"large-jpeg"')
        stop = min(stop, len(raw) - 1)
        return httpx.Response(206, headers={'content-type': 'image/jpeg',
            'content-range': f'bytes {start}-{stop}/{len(raw)}', 'etag': '"large-jpeg"'},
            content=raw[start:stop + 1])

    target = tmp_path / 'image.part'
    proof = _download('https://storage.noirlab.edu/media/archives/images/wallpaper5/test.jpg',
        target, claim, transport=httpx.MockTransport(get))
    assert target.read_bytes() == raw
    assert len(proof['parts']) == len(calls) == 513
    assert sorted(calls) == list(range(0, len(raw), PART_BYTES))


@pytest.mark.parametrize("change", ["missing_range", "range_gap", "range_stop", "zero_total", "oversize_total",
    "missing_etag", "weak_etag", "changed_etag", "changed_total", "short_body", "long_body", "later_200"])
def test_range_protocol_errors_never_retry_the_failed_range_or_fall_back(illustration, change):
    value = illustration

    def alter(start, stop, status, headers, content, raw):
        if change in {"changed_etag", "changed_total", "later_200"} and start == 0:
            return status, headers, content
        if change == "missing_range":
            headers.pop("content-range")
        elif change == "range_gap":
            headers["content-range"] = f"bytes {start + 1}-{min(stop, len(raw) - 1)}/{len(raw)}"
        elif change == "range_stop":
            headers["content-range"] = f"bytes {start}-{min(stop, len(raw) - 1) - 1}/{len(raw)}"
        elif change == "zero_total":
            headers["content-range"] = f"bytes {start}-{stop}/0"
        elif change == "oversize_total":
            headers["content-range"] = f"bytes {start}-{stop}/{PART_BYTES * MAX_PARTS + 1}"
        elif change == "missing_etag":
            headers.pop("etag")
        elif change == "weak_etag":
            headers["etag"] = 'W/"weak"'
        elif change == "changed_etag":
            headers["etag"] = '"another-version"'
        elif change == "changed_total":
            headers["content-range"] = f"bytes {start}-{min(stop, len(raw) - 1)}/{len(raw) + 1}"
        elif change == "short_body":
            content = content[:-1]
        elif change == "long_body":
            content += b"x"
        elif change == "later_200":
            status = 200
        return status, headers, content

    transport, calls, _, _ = ranged_download(value, mutate=alter)
    with pytest.raises(AgentError):
        prepare_article_image(value.core, value.article, value.record, value.policy, transport=transport)
    requested = sorted(calls)
    assert len(calls) == len(set(calls))
    with pytest.raises(AgentError, match="ARTICLE_IMAGE_DOWNLOAD_RECONCILIATION_REQUIRED"):
        prepare_article_image(value.core, value.article, value.record, value.policy, transport=transport)
    assert sorted(calls) == requested
    assert not (value.root / "data/article-images/image-record-1.proof.local.json").exists()
    assert not (value.root / "data/article-images/image-record-1.part").exists()


@pytest.mark.parametrize("status", [401, 403, 429])
def test_later_blocked_range_stops_without_new_url_or_retry(illustration, status):
    value = illustration

    def blocked(start, stop, result, headers, content, raw):
        return (result, headers, content) if start == 0 else (status, headers, b"")

    transport, calls, _, _ = ranged_download(value, mutate=blocked)
    with pytest.raises(AgentError, match="ARTICLE_IMAGE_SOURCE_BLOCKED"):
        prepare_article_image(value.core, value.article, value.record, value.policy, transport=transport)
    assert len(calls) <= 3 and len(calls) == len(set(calls))


@pytest.mark.parametrize("change", ["contract", "size", "gap", "block_hash", "truncated", "reordered"])
def test_range_proof_cannot_be_rebound_with_missing_or_tampered_blocks(illustration, change):
    value = illustration
    transport, _, _, _ = ranged_download(value)
    proof = prepare_article_image(value.core, value.article, value.record, value.policy, transport=transport)
    proof = copy.deepcopy(proof)
    if change == "contract":
        proof["download"]["contract"] = "unknown"
    elif change == "size":
        proof["download"]["bytes"] += 1
    elif change == "gap":
        proof["download"]["parts"][1]["start"] += 1
    elif change == "block_hash":
        proof["download"]["parts"][1]["sha256"] = "f" * 64
    elif change == "truncated":
        proof["download"]["parts"].pop()
    else:
        proof["download"]["parts"].reverse()
    rewrite_proof(value, proof)
    with pytest.raises(AgentError):
        validate_article_image(value.core, value.record)


def test_first_200_range_ignored_response_is_one_known_full_download(illustration):
    value = illustration
    proof, raw = prepare(value)
    assert proof["download"] == {"contract": "image-http-full-v1", "etag": None,
        "bytes": len(raw), "parts": [{"start": 0, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}]}
    assert (value.root / "data/article-images/image-record-1.claim.local.json.part-0.local.json").is_file()


@pytest.mark.parametrize("change", ["missing_claim", "claim_input", "truncated_input"])
def test_complete_cache_with_lost_or_changed_claim_does_not_restore_or_download(illustration, change):
    value = illustration
    proof, _ = prepare(value)
    path = value.root / "data/article-images/image-record-1.claim.local.json"
    if change == "missing_claim":
        path.unlink()
    else:
        data = json.loads(path.read_text())
        if change == "truncated_input":
            data["input"] = {"contract": CONTRACT, "record_id": value.record["id"]}
        else:
            data["input"]["image_url"] = "https://evil.invalid/image.jpg"
        path.write_text(json.dumps(data))
    with pytest.raises(AgentError):
        validate_article_image(value.core, value.record)
    with pytest.raises(AgentError):
        prepare_article_image(value.core, value.article, value.record, value.policy,
            transport=httpx.MockTransport(lambda _: pytest.fail("lost cache claim cannot trigger another GET")))
    assert value.record["illustration"] == proof


@pytest.mark.parametrize("change", ["policy_hash", "input_hash", "record_url", "rights", "policy_source"])
def test_invalid_input_policy_or_rights_never_reaches_download(illustration, change):
    value = illustration
    if change in {"policy_hash", "input_hash"}:
        value.record[change] = "f" * 64
    elif change == "record_url":
        value.record["url"] += "changed/"
    elif change == "rights":
        value.policy["rights_evidence"] = "https://evil.invalid/rights"
        value.record["policy_hash"] = fingerprint(value.policy)
    else:
        value.policy["source_id"] = "another-source"
        value.record["policy_hash"] = fingerprint(value.policy)
    with pytest.raises(AgentError):
        prepare_article_image(value.core, value.article, value.record, value.policy,
            transport=httpx.MockTransport(lambda _: pytest.fail("invalid binding must be blocked before GET")))
    assert not (value.root / "data/article-images/image-record-1.claim.local.json").exists()


@pytest.mark.parametrize("bad", [False, True])
def test_official_wallpaper_preferred_without_falling_back_from_bad_wallpaper_link(illustration, bad):
    value = illustration
    expected = value.image_url.replace("publicationjpg/", "wallpaper5/")
    literal = expected.replace(value.source.ident, "other-image") if bad else expected
    page = value.page.replace("</html>", '<a href="' + literal + '">2048x1536</a></html>')
    if bad:
        with pytest.raises(AgentError, match="ARTICLE_IMAGE_DOWNLOAD_REQUIRED"):
            image_candidate(value.source.source_id, value.article["url"], page)
    else:
        assert image_candidate(value.source.source_id, value.article["url"], page)["image_url"] == expected


def test_rotated_exif_does_not_pass_without_byte_preserving_orientation(illustration):
    value = illustration
    output = io.BytesIO()
    image = Image.new("RGB", (1600, 900), "navy")
    exif = image.getexif()
    exif[274] = 6
    image.save(output, format="JPEG", exif=exif)
    transport, calls, _ = download(value, raw=output.getvalue())
    with pytest.raises(AgentError, match="ARTICLE_IMAGE_QUALITY_REQUIRED"):
        prepare_article_image(value.core, value.article, value.record, value.policy, transport=transport)
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["changed_etag", "missing_etag", "weak_etag", "unquoted_etag", "full_contract",
    "missing_part_claim", "changed_part_claim", "truncated_part_claim", "merged_blocks"])
def test_saved_range_framing_requires_exact_etag_and_every_original_part_claim(illustration, change):
    value = illustration
    transport, _, raw, _ = ranged_download(value)
    proof = prepare_article_image(value.core, value.article, value.record, value.policy, transport=transport)
    proof = copy.deepcopy(proof)
    if change == "changed_etag":
        proof["download"]["etag"] = '"another-etag"'
    elif change == "missing_etag":
        proof["download"].pop("etag")
    elif change == "weak_etag":
        proof["download"]["etag"] = 'W/"weak"'
    elif change == "unquoted_etag":
        proof["download"]["etag"] = "unquoted"
    elif change == "full_contract":
        proof["download"].update(contract="image-http-full-v1", etag=None)
    elif change == "merged_blocks":
        block = raw[:PART_BYTES * 2]
        proof["download"]["parts"][:2] = [{"start": 0, "bytes": len(block), "sha256": hashlib.sha256(block).hexdigest()}]
    else:
        path = value.root / ("data/article-images/image-record-1.claim.local.json.part-" + str(PART_BYTES) + ".local.json")
        if change == "missing_part_claim":
            path.unlink()
        elif change == "changed_part_claim":
            data = json.loads(path.read_text())
            data["etag"] = '"another-etag"'
            path.write_text(json.dumps(data))
        else:
            path.write_text(json.dumps({"start": PART_BYTES, "automatic_retry": False}))
    rewrite_proof(value, proof)
    with pytest.raises(AgentError):
        validate_article_image(value.core, value.record)
    with pytest.raises(AgentError):
        prepare_article_image(value.core, value.article, value.record, value.policy,
            transport=httpx.MockTransport(lambda _: pytest.fail("lost range evidence cannot cause a GET")))


def test_full_fallback_proof_never_claims_a_range_etag(illustration):
    value = illustration
    proof, _ = prepare(value)
    proof = copy.deepcopy(proof)
    proof["download"]["etag"] = '"invented-etag"'
    rewrite_proof(value, proof)
    with pytest.raises(AgentError):
        validate_article_image(value.core, value.record)
