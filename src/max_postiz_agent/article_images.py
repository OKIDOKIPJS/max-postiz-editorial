"""Иллюстрация из точной научной статьи: права, файл и неизменяемое доказательство.

Скачивается один объявленный качественный JPEG; ссылки из повествования не
исполняются. Отказ или неизвестный результат GET сохраняется без повтора.
Старые текстовые записи не получают вложение по одному изменению настроек.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import time
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from .article_attribution import CC_BY_URL, ILLUSTRATED_PRESENTATION_CONTRACT, illustrated_presentation
from .article_sources import source_spec, validate_source_url
from .config import AgentError, fingerprint, inside
from .media import inspect_image

CONTRACT = ILLUSTRATED_PRESENTATION_CONTRACT
MAX_BYTES = 10 * 1024 * 1024
MAX_PROOF_BYTES = 20 * 1024 * 1024
HOSTS = {'noirlab_iotw': ('storage.noirlab.edu', '/media/archives/images/'),
         'esahubble_potw': ('cdn.esahubble.org', '/archives/images/')}
_PERSON = re.compile(r'\b(?:portrait|selfie|group photograph|group photo|team photo|people|person|visitors|students|astronauts?)\b', re.I)
PART_BYTES = 8192
MAX_PARTS = (MAX_BYTES + PART_BYTES - 1) // PART_BYTES


def _require(ok, code='ARTICLE_IMAGE_PROOF_INVALID'):
    if not ok:
        raise AgentError(code, 409)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _rights_contact_hash(html):
    """Только CF-кодировка контакта: все остальные исходные символы значимы."""
    offsets = [0] + [match.end() for match in re.finditer('\n', html)]
    replacements = []
    attribute = re.compile(r'''\s+([^\s/>=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?''')
    prefix = '/cdn-cgi/l/email-protection#'

    class Contacts(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if not any(tag == 'span' and key == 'data-cfemail' or tag == 'a' and key == 'href'
                       and isinstance(value, str) and value.startswith(prefix) for key, value in attrs):
                return
            _require(len(attrs) == len({key for key, _ in attrs}))
            raw = self.get_starttag_text()
            start = offsets[self.getpos()[0] - 1] + self.getpos()[1]
            position = re.match(r'<[^\s/>]+', raw).end()
            while (match := attribute.match(raw, position)) is not None:
                position = match.end()
                name = match[1].lower()
                group = next((i for i in (2, 3, 4) if match[i] is not None), None)
                if group is None:
                    _require(not (tag == 'span' and name == 'data-cfemail'))
                    continue
                value, lead = match[group], ''
                if tag == 'a' and name == 'href' and value.startswith(prefix):
                    lead, value = prefix, value[len(prefix):]
                elif not (tag == 'span' and name == 'data-cfemail'):
                    continue
                _require(8 <= len(value) <= 642 and len(value) % 2 == 0
                         and re.fullmatch('[0-9a-fA-F]+', value) is not None)
                encoded = bytes.fromhex(value)
                try:
                    email = bytes(char ^ encoded[0] for char in encoded[1:]).decode('utf-8')
                except UnicodeError as exc:
                    raise AgentError('ARTICLE_IMAGE_PROOF_INVALID', 409) from exc
                _require(email.isprintable() and re.fullmatch(r'[^@\s<>]+@[^@\s<>]+', email) is not None)
                replacements.append((start + match.start(group), start + match.end(group),
                                     lead + 'decoded-email:' + email.encode('utf-8').hex()))
            _require(re.fullmatch(r'\s*/?>', raw[position:]) is not None)

        handle_startendtag = handle_starttag

    parser = Contacts(convert_charrefs=False)
    parser.feed(html)
    parser.close()
    for start, end, value in reversed(replacements):
        html = html[:start] + value + html[end:]
    return _sha(html.encode('utf-8'))


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    if os.name != 'nt':
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _download(url, destination, claim_path, *, transport=None):
    """Ограниченный HTTP Range с If-Match, без повторов потерянного диапазона.

    Отдельные диапазоны позволяют передать официальный файл при обрывах
    длинных соединений. 403/429 не снимаются сменой соединения или URL.
    """
    started = time.monotonic()

    def part(start, stop, etag=None, total=None):
        _require(time.monotonic() - started < 180, 'ARTICLE_IMAGE_SOURCE_UNAVAILABLE')
        headers = {'Range': f'bytes={start}-{stop}', 'Accept-Encoding': 'identity'}
        if etag is not None:
            headers['If-Match'] = etag
        _write(claim_path.with_name(claim_path.name + f'.part-{start}.local.json'),
               {'start': start, 'stop': stop, 'etag': etag, 'automatic_retry': False})
        with httpx.Client(timeout=httpx.Timeout(30, connect=10), follow_redirects=False,
                          trust_env=False, transport=transport) as client:
            with client.stream('GET', url, headers=headers) as response:
                _require(response.status_code not in {401, 403, 429}, 'ARTICLE_IMAGE_SOURCE_BLOCKED')
                _require(response.status_code in {200, 206}, 'ARTICLE_IMAGE_SOURCE_UNAVAILABLE')
                _require(response.headers.get('content-type', '').split(';')[0].strip() == 'image/jpeg', 'ARTICLE_IMAGE_FORMAT_INVALID')
                ranged = response.status_code == 206
                if ranged:
                    match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('content-range', ''))
                    _require(match is not None, 'ARTICLE_IMAGE_RANGE_INVALID')
                    a, b, length = map(int, match.groups())
                    tag = response.headers.get('etag')
                    _require(length > 0 and length <= min(MAX_BYTES, PART_BYTES * MAX_PARTS), 'ARTICLE_IMAGE_SIZE_INVALID')
                    _require(tag and not tag.startswith('W/') and a == start and b == min(stop, length - 1)
                             and (etag is None or tag == etag and length == total), 'ARTICLE_IMAGE_RANGE_INVALID')
                    limit = b - a + 1
                else:
                    _require(start == 0 and etag is None, 'ARTICLE_IMAGE_RANGE_INVALID')
                    length, tag, limit = None, None, MAX_BYTES
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    _require(len(raw) <= limit, 'ARTICLE_IMAGE_SIZE_INVALID')
                    _require(time.monotonic() - started < 180, 'ARTICLE_IMAGE_SOURCE_UNAVAILABLE')
                _require(not ranged or len(raw) == limit, 'ARTICLE_IMAGE_RANGE_INVALID')
                return bytes(raw), length, tag

    first, total, etag = part(0, PART_BYTES - 1)
    pieces = [{'start': 0, 'bytes': len(first), 'sha256': _sha(first)}]
    with destination.open('xb') as output:
        output.write(first)
        if total is not None:
            ranges = [(start, min(start + PART_BYTES - 1, total - 1))
                      for start in range(len(first), total, PART_BYTES)]
            # Фиксированные пакеты по два запроса; ошибочный диапазон не повторяется.
            with ThreadPoolExecutor(max_workers=2) as executor:
                for offset in range(0, len(ranges), 2):
                    batch = ranges[offset:offset + 2]
                    futures = [executor.submit(part, a, b, etag, total) for a, b in batch]
                    results = [future.result() for future in futures]
                    for (a, _), (raw, _, _) in zip(batch, results, strict=True):
                        output.write(raw)
                        pieces.append({'start': a, 'bytes': len(raw), 'sha256': _sha(raw)})
        output.flush()
        os.fsync(output.fileno())
    _require(total is None or destination.stat().st_size == total, 'ARTICLE_IMAGE_RANGE_INVALID')
    return {'contract': 'image-http-ranges-v1' if total is not None else 'image-http-full-v1',
            'etag': etag, 'bytes': destination.stat().st_size, 'parts': pieces}


def image_candidate(source_id, url, page_html):
    """Из официального блока downloads берём файл той же статьи и полный credit."""
    _require(source_id in HOSTS, 'ARTICLE_IMAGE_SOURCE_NOT_SUPPORTED')
    validate_source_url(source_id, url)
    ident = urlsplit(url).path.rstrip('/').rsplit('/', 1)[-1]
    soup = BeautifulSoup(page_html, 'html.parser')
    credits = soup.select('.left-column div.credit')
    _require(len(credits) == 1, 'ARTICLE_IMAGE_RIGHTS_REQUIRED')
    credit = ' '.join(credits[0].get_text(' ', strip=True).split())
    from .server_science import EXCEPTION
    _require(credit and len(credit) <= 1000 and not EXCEPTION.search(credit), 'ARTICLE_IMAGE_RIGHTS_REQUIRED')
    # Не расширяем разрешение CC BY на изображения узнаваемых людей.
    column = soup.select_one('.left-column')
    _require(column is not None and not _PERSON.search(column.get_text(' ', strip=True)), 'ARTICLE_IMAGE_RIGHTS_REQUIRED')
    credit_links = []
    for anchor in credits[0].select('a[href]'):
        href = urljoin(url, anchor['href'])
        parsed = urlsplit(href)
        _require(parsed.scheme in {'https', 'http'} and parsed.hostname and not parsed.username
                 and not parsed.password and parsed.port in {None, 80, 443}, 'ARTICLE_IMAGE_RIGHTS_REQUIRED')
        if href not in credit_links:
            credit_links.append(href)
    if credit_links:
        credit += ' — ' + ' '.join(credit_links)
    _require(len(credit) <= 1000, 'ARTICLE_IMAGE_RIGHTS_REQUIRED')
    host, prefix = HOSTS[source_id]
    selected = None
    # Официальный 2048x1536 даёт качественное фото без десятков мегабайт
    # печатного оригинала. Выбор фиксируется ДО единственного запроса.
    for category, label in (('wallpaper5', '2048x1536'), ('publicationjpg', 'Publication JPEG')):
        expected = 'https://' + host + prefix + category + '/' + ident + '.jpg'
        candidates = {urljoin(url, a['href']) for a in soup.select('a[href]')
                      if a.get_text(' ', strip=True) == label}
        if candidates:
            _require(candidates == {expected}, 'ARTICLE_IMAGE_DOWNLOAD_REQUIRED')
            selected = expected
            break
    _require(selected is not None, 'ARTICLE_IMAGE_DOWNLOAD_REQUIRED')
    return {'image_url': selected, 'credit': credit, 'credit_links': credit_links}


def _source_proof(core, article, source_id):
    from .server_science import _article, _rights, _table_value, _html, _release_day
    from .server_nasa import _parser
    directory = inside(core.settings.root, 'data/server-sources')
    found = []
    if directory.is_dir():
        paths = sorted(directory.glob(source_id + '-*.local.json'))
        _require(len(paths) <= 2000)
        for path in paths:
            _require(not path.is_symlink() and path.stat().st_size <= MAX_PROOF_BYTES)
            try:
                proof = json.loads(path.read_text(encoding='utf-8'))
            except (ValueError, UnicodeError) as exc:
                raise AgentError('ARTICLE_IMAGE_PROOF_INVALID', 409) from exc
            if proof.get('article') == article:
                found.append((path, proof))
    _require(found, 'ARTICLE_IMAGE_SOURCE_PROOF_REQUIRED')
    checked = []
    for path, proof in found:
        spec = source_spec(source_id)
        _require(proof.get('source_id') == source_id and proof.get('rights_evidence') == spec['rights_evidence']
                 and proof.get('rights_text_sha256') == spec['rights_text_sha256'])
        for name in ('page', 'catalog', 'rights'):
            _require(isinstance(proof.get(name + '_html'), str)
                     and _sha(proof[name + '_html'].encode()) == proof.get('hashes', {}).get(name))
        _rights(proof['rights_html'].encode(), source_id)
        soup = _html(proof['page_html'].encode())
        # Повторяем полный разбор подписи и частных ограничений страницы.
        item = {'url': article['url'], 'title': article['title'],
                'day': _release_day(_table_value(soup, 'Release date:'))}
        parsed, details = _article(proof['page_html'].encode(), source_id, item, _parser(science_mailto=True))
        _require(parsed == article and details['credit'] == proof.get('credit'))
        if 'mailto:' in article['original']:
            _require(proof.get('extraction_contract') == 'science-mailto-v1')
        candidate = image_candidate(source_id, article['url'], proof['page_html'])
        checked.append((path, proof, candidate))
    # Несколько catalog checkpoints допустимы только при одних странице/условиях/картинке.
    _require(len({fingerprint({'page': p['hashes']['page'], 'rights': _rights_contact_hash(p['rights_html']),
                              'image': c}) for _, p, c in checked}) == 1)
    return checked[0]


def _dimensions(root, media):
    info = inspect_image(root, media['path'], MAX_BYTES)
    _require(info['sha256'] == media['sha256'])
    from PIL import Image
    with Image.open(inside(root, media['path'])) as image:
        width, height = image.size
        _require(image.format == 'JPEG' and media['mime'] == 'image/jpeg')
        _require(image.getexif().get(274, 1) == 1, 'ARTICLE_IMAGE_QUALITY_REQUIRED')
    _require(max(width, height) >= 1600 and min(width, height) >= 800, 'ARTICLE_IMAGE_QUALITY_REQUIRED')
    return {**info, 'width': width, 'height': height}


def prepare_article_image(core, article, record, policy, *, transport=None):
    """Один durable GET; при повторах читаем доказательство, не скачиваем заново."""
    marker = record.get('presentation_contract')
    _require(illustrated_presentation(marker) and marker == policy.get('presentation_contract'))
    _require(re.fullmatch(r'[A-Za-z0-9-]{1,80}', record.get('id', '')) is not None)
    _require(record.get('policy_hash') == fingerprint(policy)
             and record.get('input_hash') == fingerprint(article)
             and record.get('url') == article.get('url'))
    if record.get('illustration') is not None:
        validate_article_image(core, record)
        return copy.deepcopy(record['illustration'])
    source_id = record.get('source_id')
    _require(policy.get('source_id') == source_id and source_id in HOSTS, 'ARTICLE_IMAGE_SOURCE_NOT_SUPPORTED')
    _require(policy.get('rights_evidence') == source_spec(source_id)['rights_evidence'], 'ARTICLE_IMAGE_RIGHTS_REQUIRED')
    path, source_proof, candidate = _source_proof(core, article, source_id)
    directory = inside(core.settings.root, 'data/article-images')
    directory.mkdir(parents=True, exist_ok=True)
    claim_path = directory / (record['id'] + '.claim.local.json')
    proof_path = directory / (record['id'] + '.proof.local.json')
    binding_input = {'contract': marker, 'record_id': record['id'], 'input_hash': fingerprint(article),
        'policy_hash': fingerprint(policy), 'source_id': source_id, 'source_url': article['url'],
        'source_proof': path.relative_to(core.settings.root).as_posix(), 'source_proof_sha256': _sha(path.read_bytes()),
        'page_sha256': source_proof['hashes']['page'], 'rights_sha256': source_proof['hashes']['rights'],
        'rights_text_sha256': source_spec(source_id)['rights_text_sha256'],
        'rights_evidence': policy['rights_evidence'], 'license': CC_BY_URL, **candidate}
    if proof_path.exists():
        _require(not proof_path.is_symlink() and claim_path.is_file() and not claim_path.is_symlink())
        _require(json.loads(claim_path.read_text(encoding='utf-8')).get('input') == binding_input)
        saved = json.loads(proof_path.read_text(encoding='utf-8'))
        _require({k: saved.get(k) for k in binding_input} == binding_input)
        probe = {**record, 'illustration': saved, 'image_credit': saved['credit'],
                 'illustration_proof_hash': fingerprint(saved)}
        validate_article_image(core, probe)
        return saved
    _require(not claim_path.exists(), 'ARTICLE_IMAGE_DOWNLOAD_RECONCILIATION_REQUIRED')
    _write(claim_path, {'input': binding_input, 'started_at': time.time(), 'automatic_retry': False})
    temporary = directory / (record['id'] + '.part')
    try:
        download = _download(candidate['image_url'], temporary, claim_path, transport=transport)
        raw = temporary.read_bytes()
        media_dir = inside(core.settings.root, core.settings.app['media_dir'])
        media_dir.mkdir(parents=True, exist_ok=True)
        image_path = media_dir / (_sha(raw) + '.jpg')
        _require(not image_path.is_symlink())
        if image_path.exists():
            _require(image_path.read_bytes() == raw)
            temporary.unlink()
        else:
            os.replace(temporary, image_path)
            if os.name != 'nt':
                fd = os.open(media_dir, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        media = {'path': image_path.relative_to(core.settings.root).as_posix(),
                 'sha256': _sha(raw), 'mime': 'image/jpeg'}
        info = _dimensions(core.settings.root, media)
        proof = {**binding_input, 'media': media, **info, 'mime': 'image/jpeg',
                 'download': download, 'downloaded_at': time.time()}
        _write(proof_path, proof)
        return proof
    except (httpx.HTTPError, OSError) as exc:
        raise AgentError('ARTICLE_IMAGE_SOURCE_UNAVAILABLE', 503) from exc
    finally:
        temporary.unlink(missing_ok=True)


def validate_article_image(core, record, variant=None):
    """Полная локальная сверка перед stage/approval/send, без новых запросов."""
    marker = record.get('presentation_contract')
    metadata = getattr(variant, 'metadata_json', {}) if variant is not None else {}
    markers = (marker, metadata.get('presentation_contract'))
    if not any(value is not None for value in markers):
        _require(not record.get('illustration') and not metadata.get('illustration')
                 and (variant is None or not variant.media), 'ARTICLE_MEDIA_NOT_ALLOWED')
        return []
    _require(illustrated_presentation(marker) and (variant is None or metadata.get('presentation_contract') == marker))
    proof = record.get('illustration')
    _require(isinstance(proof, dict) and proof.get('contract') == marker
             and proof.get('record_id') == record.get('id') and proof.get('source_id') == record.get('source_id')
             and proof.get('source_url') == record.get('url') and proof.get('policy_hash') == record.get('policy_hash')
             and proof.get('input_hash') == record.get('input_hash') and proof.get('license') == CC_BY_URL)
    spec = source_spec(record['source_id'])
    _require(proof.get('rights_evidence') == spec['rights_evidence']
             and proof.get('rights_text_sha256') == spec['rights_text_sha256']
             and proof.get('mime') == 'image/jpeg')
    _require(record.get('image_credit') == proof.get('credit') and record.get('illustration_proof_hash') == fingerprint(proof))
    path = inside(core.settings.root, 'data/article-images/' + record['id'] + '.proof.local.json')
    _require(path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_PROOF_BYTES)
    _require(json.loads(path.read_text(encoding='utf-8')) == proof)
    claim_path = path.with_name(record['id'] + '.claim.local.json')
    _require(claim_path.is_file() and not claim_path.is_symlink())
    claim = json.loads(claim_path.read_text(encoding='utf-8'))
    claim_keys = ('contract', 'record_id', 'input_hash', 'policy_hash', 'source_id', 'source_url',
                  'source_proof', 'source_proof_sha256', 'page_sha256', 'rights_sha256',
                  'rights_text_sha256', 'rights_evidence', 'license', 'image_url', 'credit', 'credit_links')
    _require(isinstance(claim, dict) and claim.get('input') == {k: proof.get(k) for k in claim_keys})
    source_path = inside(core.settings.root, proof['source_proof'])
    _require(source_path.is_file() and source_path.is_relative_to(inside(core.settings.root, 'data/server-sources'))
             and not source_path.is_symlink() and _sha(source_path.read_bytes()) == proof['source_proof_sha256'])
    source_proof = json.loads(source_path.read_text(encoding='utf-8'))
    _require(fingerprint(source_proof['article']) == record['input_hash'])
    _, checked, candidate = _source_proof(core, source_proof['article'], record['source_id'])
    _require(candidate['image_url'] == proof.get('image_url') and candidate['credit'] == proof.get('credit')
             and checked['hashes']['page'] == proof.get('page_sha256')
             and checked['hashes']['rights'] == proof.get('rights_sha256'))
    media = proof.get('media')
    _require(isinstance(media, dict) and set(media) == {'path', 'sha256', 'mime'})
    canonical = Path(core.settings.app['media_dir']) / (media['sha256'] + '.jpg')
    _require(media['path'] == canonical.as_posix())
    info = _dimensions(core.settings.root, media)
    _require(all(proof.get(k) == v for k, v in info.items()))
    download = proof.get('download')
    _require(isinstance(download, dict) and download.get('contract') in {'image-http-ranges-v1', 'image-http-full-v1'}
             and download.get('bytes') == info['bytes'] and isinstance(download.get('parts'), list)
             and 0 < len(download['parts']) <= MAX_PARTS)
    raw = inside(core.settings.root, media['path']).read_bytes()
    ranged = download['contract'] == 'image-http-ranges-v1'
    if ranged:
        _require(isinstance(download.get('etag'), str)
                 and re.fullmatch(r'"[^"\r\n]{1,200}"', download['etag']) is not None
                 and len(raw) <= PART_BYTES * MAX_PARTS
                 and len(download['parts']) == (len(raw) + PART_BYTES - 1) // PART_BYTES)
    else:
        _require(download.get('etag') is None and len(download['parts']) == 1)
    at = 0
    for part in download['parts']:
        _require(isinstance(part, dict) and part.get('start') == at
                 and type(part.get('bytes')) is int and part['bytes'] > 0)
        block = raw[at:at + part['bytes']]
        _require(len(block) == part['bytes'] and _sha(block) == part.get('sha256'))
        _require(not ranged or part['bytes'] == min(PART_BYTES, len(raw) - at))
        part_path = claim_path.with_name(claim_path.name + f'.part-{at}.local.json')
        _require(part_path.is_file() and not part_path.is_symlink())
        _require(json.loads(part_path.read_text(encoding='utf-8')) == {
            'start': at, 'stop': PART_BYTES - 1 if at == 0 else min(at + PART_BYTES - 1, len(raw) - 1),
            'etag': download['etag'] if at else None, 'automatic_retry': False})
        at += part['bytes']
    _require(at == len(raw))
    if variant is not None:
        _require(variant.media == [media] and metadata.get('illustration') == proof
                 and metadata.get('illustration_proof_hash') == fingerprint(proof)
                 and metadata.get('image_credit') == proof['credit'])
        # Почтовые адреса — только literal ссылки источника, без отправки писем.
        # Старый links-v2 остаётся прежним; новый формат отдельно запрещает
        # потерю/подмену разрешённых mailto во всём переведённом тексте.
        literal_mailto = re.compile(r'mailto:[^\s<>"\]\)]+', re.I)
        _require(set(literal_mailto.findall(source_proof['article']['original']))
                 == set(literal_mailto.findall(variant.body)), 'TRANSLATION_REFERENCE_CHANGED')
    return [copy.deepcopy(media)]
