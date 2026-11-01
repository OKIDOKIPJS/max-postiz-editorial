"""Exact Pikabu preparation and conservative public-story readback.

This module never submits a post. Browser submission lives in pikabu_browser_worker;
the export remains an explicit fallback, not a successful publication.
"""

import hashlib
import html
import io
import json
import math
import re
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup, Comment

from .adapters import Delivery
from .config import AgentError, fingerprint, inside, parse_json
from .pikabu_metadata import metadata, story_url


CONTRACT = "pikabu-exact-package-v1"
READBACK_CONTRACT = "pikabu-public-story-dom-v3"
TAG_POLICY = "pikabu-native-text-tag-v1"
TAG_POLICY_SOURCE = "https://help.pikabu.ru/article/39889"
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_HASH = re.compile(r"[a-f0-9]{64}")


def editor_body(snapshot):
    """The separate Pikabu heading replaces only an exact duplicated prefix."""
    title, body = snapshot["title"], snapshot["body"]
    prefix = title + "\n\n"
    return body[len(prefix):] if body.startswith(prefix) else body


def public_tag_contract(snapshot, platform_added_tags):
    """Bind declared tags separately from one documented native format label.

    https://help.pikabu.ru/article/39889, section 4: Pikabu can add «Текст»
    automatically to text-only posts. This never authorizes editorial extras.
    """
    declared = sorted(tag.strip().casefold() for tag in metadata(
        snapshot["title"], snapshot.get("metadata"))["tags"])
    if (not isinstance(platform_added_tags, list)
            or not (platform_added_tags == [] or (
                platform_added_tags == ["Текст"] and snapshot.get("media") == []
                and "текст" not in declared))):
        raise AgentError("PIKABU_READBACK_TAGS_MISMATCH")
    effective = sorted(declared + [tag.casefold() for tag in platform_added_tags])
    return {"tag_policy": TAG_POLICY, "tag_policy_source": TAG_POLICY_SOURCE,
            "tags_sha256": fingerprint(declared),
            "declared_tags_sha256": fingerprint(declared),
            "effective_tags_sha256": fingerprint(effective),
            "platform_added_tags": list(platform_added_tags)}


def check_body_links(links, body, *, base_url, sources=(), image_urls=()):
    """Plain body permits only its explicit URLs and exact source/image targets."""
    found = re.findall(r"https?://[^\s<>\"']+", body)
    allowed = set(found) | {url.rstrip(".,;:!?)]}") for url in found} | set(image_urls)
    allowed.update(source.get("url") for source in sources if isinstance(source, dict) and source.get("url"))
    for href in links:
        target = urljoin(base_url, href)
        parsed = urlsplit(target)
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            raise AgentError("PIKABU_UNAPPROVED_LINK")
        if target in allowed:
            continue
        query = parse_qs(parsed.query, keep_blank_values=True)
        if (parsed.scheme == "https" and not parsed.fragment and parsed.netloc in {"pikabu.ru", "www.pikabu.ru"}
                and parsed.path == urlsplit(base_url).path and set(query) == {"u"}
                and len(query["u"]) == 1 and query["u"][0] in allowed):
            continue
        raise AgentError("PIKABU_UNAPPROVED_LINK")


def owner_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
        raise AgentError("PIKABU_OWNER_REQUIRED")
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _units(value):
    try:
        return len(value.encode("utf-16-le")) // 2
    except UnicodeError as exc:
        raise AgentError("PIKABU_TEXT_INVALID") from exc


class PikabuDelivery:
    mode = "manual"

    def __init__(self, root, options=None):
        self.root = Path(root).resolve()
        self.options = options or {}

    def validate(self, destination, snapshot):
        if (not isinstance(destination, str) or not destination or len(destination) > 200
                or any(ord(char) < 32 for char in destination)):
            raise AgentError("PIKABU_DESTINATION_INVALID")
        if not isinstance(snapshot, dict):
            raise AgentError("PIKABU_SNAPSHOT_INVALID")
        title, body = snapshot.get("title"), snapshot.get("body")
        values = snapshot.get("metadata", {})
        if not isinstance(values, dict):
            raise AgentError("PIKABU_SNAPSHOT_INVALID")
        meta = metadata(title, values)
        if not 2 <= _units(title) <= 140:
            raise AgentError("PIKABU_TITLE_INVALID")
        if _units(",".join(meta["tags"])) > 250 or any(_units(tag) > 50 for tag in meta["tags"]):
            raise AgentError("PIKABU_TAGS_INVALID")
        if (not isinstance(body, str) or not body.strip() or "\x00" in body
                or len(body) > self.options.get("max_chars", 15000)):
            raise AgentError("PIKABU_BODY_INVALID")
        if not editor_body(snapshot).strip():
            raise AgentError("PIKABU_BODY_INVALID")
        media = snapshot.get("media", [])
        if not isinstance(media, list) or len(media) > 10:
            raise AgentError("PIKABU_MEDIA_INVALID")
        checked = []
        for index, item in enumerate(media):
            if (not isinstance(item, dict) or not isinstance(item.get("path"), str)
                    or Path(item["path"]).is_absolute()
                    or not isinstance(item.get("sha256"), str)
                    or not _HASH.fullmatch(item["sha256"])):
                raise AgentError("PIKABU_MEDIA_INVALID")
            path = inside(self.root, item["path"])
            mime = item.get("mime")
            if mime not in {"image/jpeg", "image/png"}:
                raise AgentError("PIKABU_IMAGE_REQUIRED")
            from .media import inspect_image
            info = inspect_image(self.root, item["path"])
            if info["sha256"] != item["sha256"]:
                raise AgentError("PIKABU_IMAGE_HASH_CHANGED")
            from PIL import Image
            with Image.open(path) as image:
                expected = "image/jpeg" if image.format == "JPEG" else "image/png"
                if image.width > 2048:
                    raise AgentError("PIKABU_IMAGE_PREAPPROVAL_RESIZE_REQUIRED")
            if expected != mime:
                raise AgentError("PIKABU_IMAGE_MIME_INVALID")
            suffix = ".jpg" if mime == "image/jpeg" else ".png"
            checked.append({"name": f"media-{index}{suffix}", "path": item["path"],
                            "sha256": info["sha256"], "bytes": info["bytes"], "mime": mime})
        return {"title": title, "body": editor_body(snapshot), **meta, "media": checked,
                "destination": destination, "snapshot_sha256": fingerprint(snapshot)}

    def prepare(self, snapshot, request_id, snapshot_hash, due_at):
        if not isinstance(request_id, str) or not _ID.fullmatch(request_id):
            raise AgentError("PIKABU_REQUEST_ID_INVALID")
        if (not isinstance(snapshot_hash, str) or not _HASH.fullmatch(snapshot_hash)
                or isinstance(due_at, bool) or not isinstance(due_at, (int, float))
                or not math.isfinite(due_at) or due_at < 0
                or fingerprint({"content": snapshot, "due_at": due_at}) != snapshot_hash):
            raise AgentError("PIKABU_APPROVAL_STALE", 409)
        plan = self.validate(snapshot.get("destination", ""), snapshot)
        directory = inside(self.root, "data/exports/" + request_id)
        binding = {"contract": CONTRACT, "dispatch_id": request_id,
                   "snapshot_hash": snapshot_hash, "due_at": due_at,
                   "snapshot_sha256": fingerprint(snapshot)}
        if directory.exists():
            self.verify_package(directory, expected=binding)
            return self._prepared(request_id, binding)
        # The directory itself is a durable claim. Incomplete exports are never
        # silently overwritten by another run with the same dispatch ID.
        directory.mkdir(parents=True, exist_ok=False)
        files = {}
        for item in plan["media"]:
            raw = inside(self.root, item["path"]).read_bytes()
            if _sha(raw) != item["sha256"]:
                raise AgentError("PIKABU_IMAGE_HASH_CHANGED")
            files[item["name"]] = raw
        post = {**binding, "snapshot": snapshot, "prepared": plan,
                "state": "awaiting_manual", "live_verified": False}
        files["post.json"] = _json(post)
        files["post.txt"] = (plan["title"] + "\n\n" + plan["body"]
                             + "\n\nТеги: " + ", ".join(plan["tags"])).encode("utf-8")
        figures = "".join('<img src="' + item["name"] + '" alt="">'
                          for item in plan["media"])
        article = (figures + "<h1>" + html.escape(plan["title"]) + "</h1><div class=body>"
                   + html.escape(plan["body"]).replace("\n", "<br>\n") + "</div>")
        files["post.html"] = ("<!doctype html><html lang=ru><meta charset=utf-8>"
                              "<title>Пост Пикабу</title><body>" + article
                              + "</body></html>").encode("utf-8")
        files["preview.html"] = self._preview(plan, article).encode("utf-8")
        manifest = {**binding, "files": {name: _sha(raw) for name, raw in files.items()}}
        files["manifest.json"] = _json(manifest)
        for name, raw in files.items():
            with (directory / name).open("xb") as stream:
                stream.write(raw)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, raw in files.items():
                archive.writestr(name, raw)
        with (directory / "package.zip").open("xb") as stream:
            stream.write(buffer.getvalue())
        self.verify_package(directory, expected=binding)
        return self._prepared(request_id, binding)

    @staticmethod
    def _preview(plan, article):
        textareas = "".join(
            '<label>' + label + '<textarea id="' + ident + '" readonly>'
            + html.escape(value) + '</textarea></label><button data-copy="' + ident
            + '">Копировать</button>'
            for label, ident, value in [("Заголовок", "title", plan["title"]),
                                       ("Текст", "body", plan["body"]),
                                       ("Теги", "tags", ", ".join(plan["tags"]))])
        return ("<!doctype html><html lang=ru><meta charset=utf-8>"
                '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
                "img-src 'self' data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                'connect-src \'none\'; form-action \'none\'; base-uri \'none\'">'
                "<title>Подготовленный пост Пикабу</title><style>body{font:17px system-ui;"
                "max-width:900px;margin:24px auto;padding:16px}img{max-width:100%}"
                "textarea{display:block;width:100%;height:110px}button{margin:8px 0 20px}"
                ".body{line-height:1.6}</style><h1>Пост подготовлен</h1>"
                "<p>Это локальный пакет, он ничего не публикует. Для ручного маршрута "
                "перенесите изображение, заголовок, текст и теги в редактор Пикабу. "
                "Согласование относится к точной версии, адресату и дате.</p>"
                + textareas + "<hr>" + article
                + "<script>document.querySelectorAll('[data-copy]').forEach(b=>"
                "b.addEventListener('click',async()=>{const t=document.getElementById("
                "b.dataset.copy);t.focus();t.select();try{await navigator.clipboard.writeText("
                "t.value)}catch(e){document.execCommand('copy')}}));</script></html>")

    def _prepared(self, request_id, binding):
        return Delivery("awaiting_manual", verified=False, evidence={
            "kind": "manual_package", **binding,
            "package": "data/exports/" + request_id + "/package.zip",
            "preview": "data/exports/" + request_id + "/preview.html"})

    def verify_package(self, directory, expected=None):
        directory = Path(directory).resolve()
        if not directory.is_relative_to(inside(self.root, "data/exports")):
            raise AgentError("PIKABU_PACKAGE_PATH_INVALID")
        try:
            manifest = parse_json((directory / "manifest.json").read_text(encoding="utf-8"))
            if (not isinstance(manifest, dict) or manifest.get("contract") != CONTRACT
                    or not isinstance(manifest.get("files"), dict)):
                raise ValueError
            if expected and any(manifest.get(key) != value for key, value in expected.items()):
                raise AgentError("PIKABU_PACKAGE_CONFLICT", 409)
            names = set(manifest["files"]) | {"manifest.json", "package.zip"}
            if set(path.name for path in directory.iterdir()) != names:
                raise ValueError
            for name, hashed in manifest["files"].items():
                if (not isinstance(name, str) or Path(name).name != name
                        or not _HASH.fullmatch(hashed)
                        or _sha((directory / name).read_bytes()) != hashed):
                    raise ValueError
            with zipfile.ZipFile(directory / "package.zip") as archive:
                expected_names = names - {"package.zip"}
                if (set(archive.namelist()) != expected_names
                        or len(archive.namelist()) != len(expected_names)
                        or any(archive.read(name) != (directory / name).read_bytes()
                               for name in expected_names)):
                    raise ValueError
            post = parse_json((directory / "post.json").read_text(encoding="utf-8"))
            if (fingerprint(post["snapshot"]) != manifest["snapshot_sha256"]
                    or fingerprint({"content": post["snapshot"], "due_at": post["due_at"]})
                    != manifest["snapshot_hash"]):
                raise ValueError
            return manifest
        except AgentError:
            raise
        except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile) as exc:
            raise AgentError("PIKABU_PACKAGE_DAMAGED", 409) from exc


def parse_public_story(raw, url, snapshot, expected_owner, *, encoding=None):
    """Read only the unique canonical story; comments and recommendations cannot match."""
    url = story_url(url)
    expected_owner = owner_name(expected_owner)
    if not isinstance(raw, bytes) or not raw or len(raw) > 5 * 1024 * 1024:
        raise AgentError("PIKABU_READBACK_INVALID")
    soup = BeautifulSoup(raw, "html.parser", from_encoding=encoding)
    canonicals = soup.select('link[rel="canonical"][href]')
    if len(canonicals) != 1 or story_url(canonicals[0]["href"]) != url:
        raise AgentError("PIKABU_READBACK_WRONG_STORY")
    ident = url.rsplit("_", 1)[1]
    articles = soup.select('article.story[data-story-id="' + ident + '"]')
    if len(articles) != 1:
        raise AgentError("PIKABU_READBACK_AMBIGUOUS")
    article = articles[0]
    structured = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = parse_json(script.string or script.get_text())
        except AgentError:
            continue
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, dict) and item.get("@type") == "Article" and item.get("url") == url:
                structured.append(item)
    if len(structured) != 1 or structured[0].get("headline") != snapshot.get("title"):
        raise AgentError("PIKABU_READBACK_TITLE_MISMATCH")
    headings = article.select(".story__title .story__title-link")
    if len(headings) != 1 or headings[0].get_text() != snapshot.get("title"):
        raise AgentError("PIKABU_READBACK_TITLE_MISMATCH")
    author = structured[0].get("author", {})
    authors = article.select(".story__user-link[href]")
    expected_path = "/@" + expected_owner
    author_url = urlsplit(author.get("url", "")) if isinstance(author, dict) else None
    displayed_url = urlsplit(authors[0]["href"]) if len(authors) == 1 else None
    if (not isinstance(author, dict) or author.get("name") != expected_owner
            or author_url.scheme != "https" or author_url.netloc not in {"pikabu.ru", "www.pikabu.ru"}
            or author_url.path != expected_path
            or len(authors) != 1 or authors[0].get_text(strip=True) != expected_owner
            or displayed_url.scheme not in {"", "https"}
            or (displayed_url.netloc and displayed_url.netloc not in {"pikabu.ru", "www.pikabu.ru"})
            or displayed_url.path != expected_path):
        raise AgentError("PIKABU_READBACK_OWNER_MISMATCH")
    contents = article.select(".story__content-inner")
    if len(contents) != 1:
        raise AgentError("PIKABU_READBACK_INVALID")
    content = contents[0]
    # Only paragraph blocks, explicit line breaks, and image figures are admitted.
    # Unknown widgets/hidden blocks require a new reviewed parser contract.
    blocks = content.find_all(recursive=False)
    if any(not ({"story-block_type_text", "story-block_type_image"} & set(node.get("class", [])))
           for node in blocks) or content.select("script,style,iframe,video"):
        raise AgentError("PIKABU_READBACK_UNSUPPORTED_BLOCK")
    # A correct paragraph must not hide additional published text in a sibling
    # widget or caption that the exact paragraph parser would otherwise ignore.
    for text in content.find_all(string=True):
        if (not isinstance(text, Comment) and text.strip()
                and not any(parent.name == "p" for parent in text.parents)):
            raise AgentError("PIKABU_READBACK_UNSUPPORTED_BLOCK")
    if snapshot.get("media"):
        first_text = next((index for index, node in enumerate(blocks)
                           if "story-block_type_text" in node.get("class", [])), len(blocks))
        if any(index >= first_text for index, node in enumerate(blocks)
               if "story-block_type_image" in node.get("class", [])):
            raise AgentError("PIKABU_READBACK_IMAGE_POSITION_MISMATCH")
    paragraphs = content.select(".story-block_type_text p")
    if not paragraphs:
        raise AgentError("PIKABU_READBACK_INVALID")
    pieces = []
    for node in paragraphs:
        for br in node.find_all("br"):
            br.replace_with("\n")
        pieces.append(node.get_text("", strip=False))
    body = "\n\n".join(pieces)
    if body != editor_body(snapshot):
        raise AgentError("PIKABU_READBACK_BODY_MISMATCH")
    expected_tags = metadata(snapshot["title"], snapshot.get("metadata"))["tags"]
    actual_tags = [node.get_text(strip=True) for node in article.select(".tags__tag")]
    declared = sorted(tag.strip().casefold() for tag in expected_tags)
    observed = sorted(tag.strip().casefold() for tag in actual_tags)
    added = [] if observed == declared else ["Текст"]
    tag_contract = public_tag_contract(snapshot, added)
    if fingerprint(observed) != tag_contract["effective_tags_sha256"]:
        raise AgentError("PIKABU_READBACK_TAGS_MISMATCH")
    images = content.select(".story-image__image")
    if (len(images) != len(snapshot.get("media", []))
            or len(content.select("img")) != len(images)):
        raise AgentError("PIKABU_READBACK_IMAGE_MISMATCH")
    image_urls = []
    for node in images:
        value = node.get("data-large-image") or node.get("data-src") or node.get("src", "")
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not re.fullmatch(r"cs\d*\.pikabu\.ru", parsed.netloc)
                or parsed.username or parsed.password or parsed.fragment or parsed.query):
            raise AgentError("PIKABU_READBACK_IMAGE_INVALID")
        image_urls.append(value)
    links = [node["href"] for node in content.select("a[href]")]
    for link in list(links):
        parsed = urlsplit(link)
        if (parsed.netloc in {"pikabu.ru", "www.pikabu.ru"}
                and parsed.path == urlsplit(url).path):
            redirected = parse_qs(parsed.query).get("u", [])
            if len(redirected) == 1:
                links.append(redirected[0])
    for source in snapshot.get("sources", []):
        link = source.get("url")
        if link and link not in links:
            raise AgentError("PIKABU_READBACK_SOURCE_MISMATCH")
    check_body_links([node["href"] for node in content.select("a[href]")], editor_body(snapshot),
                     base_url=url, sources=snapshot.get("sources", []), image_urls=image_urls)
    return {"contract": READBACK_CONTRACT, "external_id": ident, "permalink": url,
            "owner": expected_owner, "body_sha256": _sha(body.encode("utf-8")),
            "title_sha256": _sha(snapshot["title"].encode("utf-8")),
            **tag_contract,
            "source_urls_sha256": fingerprint([source.get("url") for source in snapshot.get("sources", [])]),
            "snapshot_sha256": fingerprint(snapshot), "image_urls": image_urls,
            "html_sha256": _sha(raw), "verification": "text_owner_links_tags_exact"}
