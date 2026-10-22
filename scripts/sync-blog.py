#!/usr/bin/env python3
"""Mirror https://sidjashnani.substack.com to static pages under /blog/.

Substack's RSS feed only carries recent essays, so this reads
/api/v1/archive (paged) and then /api/v1/posts/<slug> for the body.

A failed or suspiciously short archive leaves the published files
untouched. A single essay that cannot be fetched is kept as it was, or
left out if it has never been published here. Nothing is deleted unless
the archive was read through to an empty page.

The public pages never include a post whose title, subtitle, or body
mentions EOS (word boundary, any case, including EOS®) or Rekruuto.
superhuman.com/book links are removed from bodies. The skip list is
blog/skipped.json.

Images are stored as WebP under assets/blog/<slug>/, fetched from
Substack's image CDN already resized (the source PNGs are often about
2MB each). Cover images also get a JPEG for Open Graph, because some
share previews still handle JPEG more reliably than WebP. Audio is not
downloaded: the player points at Substack's upload URL, which redirects
to a short-lived file, and the essay links to the original.

Usage:
  python3 scripts/sync-blog.py --self-test
  python3 scripts/sync-blog.py            # fetch and write
  python3 scripts/sync-blog.py --refresh  # ignore the local HTTP cache

The GitHub Action commits to main only when this script changes files.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
SITE = "https://sidjashnani.com"
ARCHIVE = "https://sidjashnani.substack.com/api/v1/archive"
POST_URL = "https://sidjashnani.substack.com/api/v1/posts/{slug}"
SUBSCRIBE = "https://sidjashnani.substack.com/welcome"
UA = "sidjashnani.com-blog-sync/1.0 (+https://sidjashnani.com/blog/)"
LONDON = ZoneInfo("Europe/London")
CACHE = Path(os.environ.get("BLOG_CACHE", "/tmp/sid-blog-cache"))
TEMPLATE_VERSION = 1
IMAGE_WIDTH = 1400

# Word-boundary match. Underscore counts as a word character, so a random
# token like a_file_hash does not trip the rule. EOS® does, because ® is
# not a word character. E.O.S. is the same name with dots.
EOS_RE = re.compile(r"(?i)\bEOS\b|(?<![A-Za-z])E\.O\.S\.?(?![A-Za-z])")
REKRUUTO_RE = re.compile(r"(?i)\bRekruuto\b")
SUPERHUMAN_RE = re.compile(r"(?i)(?:https?://)?(?:www\.)?superhuman\.com/book\S*")
ADVISOR_A_RE = re.compile(r"\b[Aa] Certified Outgrow Advisor\b")
ADVISOR_RE = re.compile(r"\bCertified Outgrow Advisor\b")
ADVISOR_SALES_RE = re.compile(r"\bCertified Outgrow Sales Advisors?\b")
ADVISOR_PLURAL_RE = re.compile(r"\bCertified Outgrow Advisors\b")
SHARE_FOOTER_RE = re.compile(r"(?i)^thanks for reading\b.*\bshare it\b")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MARKER_CHARS_RE = re.compile(r"<!--\s*sid-blog-[a-z-]+:(?:start|end)\s*-->")

DROP_TAGS = {
    "script", "style", "button", "svg", "form", "input", "textarea",
    "select", "canvas", "noscript", "link", "meta", "source", "iframe",
}
UNWRAP_TAGS = {
    "div", "span", "section", "article", "header", "footer", "picture",
    "font", "center", "small", "label", "figcaption", "mark",
}
KEEP_TAGS = {
    "p", "blockquote", "ul", "ol", "li", "strong", "em", "a", "br", "hr",
    "sub", "sup", "code", "pre", "h2", "h3", "h4", "figure", "img",
    "table", "thead", "tbody", "tr", "th", "td",
}
VOID_TAGS = {"br", "hr", "img"}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

TYPE_LABEL = {
    "newsletter": "Letter",
    "podcast": "Podcast",
    "video": "Video",
    "thread": "Note",
}

PERSON = {
    "@type": "Person",
    "@id": f"{SITE}/#sid",
    "name": "Sid Jashnani",
    "url": f"{SITE}/",
    "jobTitle": [
        "Keynote Speaker",
        "CEO of Outgrow",
        "Outgrow Certified Advisor",
    ],
    "worksFor": {
        "@type": "Organization",
        "name": "Outgrow",
        "url": "https://runoutgrow.com",
    },
    "sameAs": [
        "https://runoutgrow.com/advisor/sid-jashnani",
        "https://sidjashnani.substack.com",
        "https://www.linkedin.com/in/sidjash/",
        "https://www.instagram.com/sidjashnani/",
        "https://x.com/siddjash",
    ],
}


class SyncError(Exception):
    """The archive could not be read safely. Callers must not write."""


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def scrub_text(value: str) -> str:
    value = value.replace("\xa0", " ")
    value = SUPERHUMAN_RE.sub("", value)
    value = ADVISOR_SALES_RE.sub(
        lambda match: "Outgrow Certified Advisors" if match.group(0).endswith("s") else "Outgrow Certified Advisor",
        value,
    )
    value = ADVISOR_A_RE.sub(
        lambda match: ("An" if match.group(0)[0] == "A" else "an") + " Outgrow Certified Advisor",
        value,
    )
    value = ADVISOR_PLURAL_RE.sub("Outgrow Certified Advisors", value)
    value = ADVISOR_RE.sub("Outgrow Certified Advisor", value)
    value = MARKER_CHARS_RE.sub("", value)
    return value


def fix_article(value: str) -> str:
    """'a Outgrow Certified Advisor' is ungrammatical after the title rewrite."""
    value = re.sub(r"\b[Aa] (?=(?:<a\b[^>]*>)?Outgrow Certified Advisor\b)", lambda match: "An " if match.group(0)[0] == "A" else "an ", value)
    value = re.sub(r"\b[Aa] (?=\[Outgrow Certified Advisor\])", lambda match: "An " if match.group(0)[0] == "A" else "an ", value)
    return value


def collapse(value: str) -> str:
    return re.sub(r"\s+", " ", scrub_text(value or "")).strip()


def exclusion_reasons(title: str, subtitle: str, body: str) -> list[str]:
    reasons = []
    fields = (("title", title), ("subtitle", subtitle), ("body", body))
    for name, text in fields:
        if text and EOS_RE.search(text):
            reasons.append(f"Mentions EOS in the {name}")
        if text and REKRUUTO_RE.search(text):
            reasons.append(f"Mentions Rekruuto in the {name}")
    return reasons


def usable_subtitle(value: str) -> str:
    """Substack often cuts description text around 200 characters, mid-sentence."""
    text = collapse(value)
    if not text:
        return ""
    if len(text) >= 190 and text[-1] not in ".!?…\"'”’":
        return ""
    return text


def first_sentence(text: str, limit: int = 180) -> str:
    text = collapse(text)
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", text)
    chosen = ""
    for part in parts:
        if not chosen:
            chosen = part
        elif len(chosen) < 80:
            chosen = f"{chosen} {part}"
        else:
            break
    if len(chosen) <= limit:
        return chosen
    cut = chosen[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:")
    return f"{cut}…"


def strip_tags(fragment: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", fragment or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return collapse(html.unescape(text))


def body_plain_for_rules(raw_html: str) -> str:
    """Text a reader could meet, plus image alt text. Ignores tags and URLs."""
    alts = []
    for match in re.finditer(r'(?i)\balt\s*=\s*"([^"]*)"', raw_html or ""):
        alts.append(html.unescape(match.group(1)))
    for match in re.finditer(r'"alt"\s*:\s*"((?:\\.|[^"\\])*)"', raw_html or ""):
        alts.append(bytes(match.group(1), "utf-8").decode("unicode_escape", errors="replace"))
    return collapse(strip_tags(raw_html or "") + " " + " ".join(alts))


def first_paragraphs(raw_html: str) -> str:
    paragraphs = re.findall(r"(?is)<p[^>]*>(.*?)</p>", raw_html or "")
    chunks = []
    for paragraph in paragraphs:
        text = strip_tags(paragraph)
        if not text or text.startswith("#"):
            continue
        chunks.append(text)
        if len(" ".join(chunks)) >= 80:
            break
    return " ".join(chunks)


def london_date(iso_value: str) -> tuple[str, str]:
    dt = datetime.fromisoformat(iso_value.replace("Z", "+00:00")).astimezone(LONDON)
    return dt.strftime("%Y-%m-%d"), dt.strftime("%-d %B %Y")


def label_for(post_type: str) -> str:
    if not post_type:
        return ""
    return TYPE_LABEL.get(post_type, "")


def json_script(data: dict) -> str:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def esc(value: str) -> str:
    return html.escape(collapse(value), quote=True)


def write_if_changed(path: Path, data: bytes) -> bool:
    if path.exists() and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return True


def write_text(path: Path, text: str) -> bool:
    if not text.endswith("\n"):
        text += "\n"
    return write_if_changed(path, text.encode("utf-8"))


def replace_between(text: str, start: str, end: str, inner: str) -> str:
    begin = text.find(start)
    finish = text.find(end, begin + len(start) if begin >= 0 else 0)
    if begin < 0 or finish < 0:
        raise SyncError(f"Missing marker {start}")
    body = inner.strip("\n")
    block = f"{start}\n{body}\n{end}" if body else f"{start}\n{end}"
    return text[:begin] + block + text[finish + len(end):]


def require_markers() -> None:
    needed = {
        ROOT / "index.html": ("<!-- sid-blog-latest:start -->", "<!-- sid-blog-latest:end -->"),
        ROOT / "sitemap.xml": ("<!-- sid-blog-sitemap:start -->", "<!-- sid-blog-sitemap:end -->"),
        ROOT / "llms.txt": ("<!-- sid-blog-index:start -->", "<!-- sid-blog-index:end -->"),
        ROOT / "llms-full.txt": ("<!-- sid-blog-full:start -->", "<!-- sid-blog-full:end -->"),
    }
    for path, (start, end) in needed.items():
        text = path.read_text(encoding="utf-8")
        if start not in text or end not in text:
            raise SyncError(f"{path.name} is missing a blog marker")


def get_bytes(url: str, accept: str) -> bytes:
    last_error: Exception | None = None
    for attempt in range(4):
        request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            last_error = error
            if error.code not in {429, 500, 502, 503, 504} or attempt == 3:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            last_error = error
            if attempt == 3:
                raise
        time.sleep(1.5 * (attempt + 1))
    raise SyncError(f"Could not fetch {url}: {last_error}")


def get_json(url: str, refresh: bool) -> object:
    CACHE.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    cache_path = CACHE / f"{key}.json"
    if cache_path.exists() and not refresh:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    payload = json.loads(get_bytes(url, "application/json").decode("utf-8"))
    cache_path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def fetch_archive(refresh: bool) -> list[dict]:
    posts: list[dict] = []
    seen: set[str] = set()
    offset = 0
    for _page in range(40):
        url = f"{ARCHIVE}?sort=new&offset={offset}&limit=50"
        try:
            payload = get_json(url, refresh)
        except Exception as error:
            raise SyncError(f"Archive request failed at offset {offset}: {error}") from error
        if not isinstance(payload, list):
            raise SyncError("Archive response was not a list")
        if not payload:
            return posts
        fresh = [item for item in payload if item.get("slug") not in seen]
        if not fresh:
            raise SyncError("Archive pagination repeated a page. Leaving the mirror untouched.")
        for item in fresh:
            seen.add(item.get("slug") or "")
            posts.append(item)
        offset += len(payload)
        time.sleep(0.15)
    raise SyncError("Archive did not end after 40 pages. Leaving the mirror untouched.")


def fetch_post(slug: str, refresh: bool) -> dict:
    url = POST_URL.format(slug=urllib.parse.quote(slug))
    payload = get_json(url, refresh)
    if not isinstance(payload, dict) or not payload.get("body_html") and not payload.get("title"):
        raise SyncError(f"Post {slug} did not return a document")
    time.sleep(0.12)
    return payload


def webp_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    chunk = data[12:16]
    if chunk == b"VP8X":
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    if chunk == b"VP8L" and len(data) >= 25:
        b0, b1, b2, b3 = data[21:25]
        width = 1 + (((b1 & 0x3F) << 8) | b0)
        height = 1 + (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6))
        return width, height
    if chunk == b"VP8 ":
        start = data.find(b"\x9d\x01\x2a")
        if start >= 0 and start + 7 <= len(data):
            width = int.from_bytes(data[start + 3:start + 5], "little") & 0x3FFF
            height = int.from_bytes(data[start + 5:start + 7], "little") & 0x3FFF
            return width, height
    return None


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in {0xC0, 0xC1, 0xC2}:
            height = int.from_bytes(data[index + 5:index + 7], "big")
            width = int.from_bytes(data[index + 7:index + 9], "big")
            return width, height
        if marker in {0xD8, 0xD9}:
            index += 2
            continue
        if index + 4 > len(data):
            break
        length = int.from_bytes(data[index + 2:index + 4], "big")
        if length < 2:
            break
        index += 2 + length
    return None


def cdn_image(origin: str, fmt: str) -> str:
    encoded = urllib.parse.quote(origin.split("?", 1)[0], safe="")
    return (
        "https://substackcdn.com/image/fetch/"
        f"w_{IMAGE_WIDTH},c_limit,f_{fmt},q_auto:good,fl_progressive:steep/{encoded}"
    )


class ImageStore:
    def __init__(self, slug: str, previous: dict):
        self.slug = slug
        self.previous = previous or {}
        self.saved: dict[str, dict] = {}

    def _path_ok(self, web_path: str, magic: bytes) -> bool:
        if not web_path or not web_path.startswith("/"):
            return False
        file_path = ROOT / web_path.lstrip("/")
        if not file_path.is_file() or file_path.stat().st_size < 400:
            return False
        head = file_path.read_bytes()[:12]
        return head.startswith(magic)

    def fetch(self, origin: str, want_jpeg: bool) -> dict | None:
        origin = (origin or "").split("?", 1)[0]
        if not origin.startswith("https://"):
            return None
        if origin in self.saved:
            record = self.saved[origin]
            if want_jpeg and not record.get("jpg"):
                pass
            else:
                return record
        prior = self.previous.get(origin) or {}
        webp_ok = self._path_ok(prior.get("webp") or "", b"RIFF")
        jpg_ok = (not want_jpeg) or self._path_ok(prior.get("jpg") or "", b"\xff\xd8")
        if webp_ok and jpg_ok and prior.get("width") and prior.get("height"):
            record = {
                "webp": prior["webp"],
                "jpg": prior.get("jpg"),
                "width": prior["width"],
                "height": prior["height"],
                "remote": False,
            }
            self.saved[origin] = record
            return record

        digest = hashlib.sha256(origin.encode("utf-8")).hexdigest()[:16]
        folder = ROOT / "assets" / "blog" / self.slug
        folder.mkdir(parents=True, exist_ok=True)
        webp_file = folder / f"{digest}.webp"
        jpg_file = folder / f"{digest}.jpg"
        try:
            webp = get_bytes(cdn_image(origin, "webp"), "image/webp,*/*")
        except Exception as error:
            log(f"  image failed {origin}: {error}")
            return {
                "webp": cdn_image(origin, "webp"),
                "jpg": cdn_image(origin, "jpg") if want_jpeg else None,
                "width": None,
                "height": None,
                "remote": True,
            }
        if not webp.startswith(b"RIFF") or webp[8:12] != b"WEBP":
            log(f"  image was not WebP: {origin}")
            return None
        write_if_changed(webp_file, webp)
        jpg_web = None
        size = webp_size(webp)
        if want_jpeg:
            try:
                jpeg = get_bytes(cdn_image(origin, "jpg"), "image/jpeg,*/*")
                if jpeg[:2] == b"\xff\xd8":
                    write_if_changed(jpg_file, jpeg)
                    jpg_web = f"/assets/blog/{self.slug}/{digest}.jpg"
                    size = jpeg_size(jpeg) or size
            except Exception as error:
                log(f"  cover jpeg failed {origin}: {error}")
        record = {
            "webp": f"/assets/blog/{self.slug}/{digest}.webp",
            "jpg": jpg_web,
            "width": size[0] if size else None,
            "height": size[1] if size else None,
            "remote": False,
        }
        self.saved[origin] = record
        return record

    def prune(self) -> None:
        folder = ROOT / "assets" / "blog" / self.slug
        if not folder.is_dir():
            return
        keep = set()
        for record in self.saved.values():
            for key in ("webp", "jpg"):
                web = record.get(key)
                if web and web.startswith("/assets/"):
                    keep.add(Path(web).name)
        for path in folder.iterdir():
            if path.is_file() and path.name not in keep:
                path.unlink()
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()


class FragmentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = {"tag": None, "attrs": {}, "children": []}
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = {"tag": tag.lower(), "attrs": {key: value or "" for key, value in attrs}, "children": []}
        self.stack[-1]["children"].append(node)
        if tag.lower() not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = {"tag": tag.lower(), "attrs": {key: value or "" for key, value in attrs}, "children": []}
        self.stack[-1]["children"].append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index]["tag"] == tag:
                self.stack = self.stack[:index]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1]["children"].append(data)

    def handle_comment(self, data: str) -> None:
        return


def parse_fragment(raw_html: str) -> dict:
    parser = FragmentParser()
    parser.feed(raw_html or "")
    parser.close()
    return parser.root


def class_list(node: dict) -> str:
    return node["attrs"].get("class", "")


def find_images(node: dict) -> list[dict]:
    found = []
    if isinstance(node, str):
        return found
    if node["tag"] == "img":
        found.append(node)
    for child in node["children"]:
        if isinstance(child, dict):
            found.extend(find_images(child))
    return found


def image_meta(node: dict) -> dict:
    meta = {}
    raw = node["attrs"].get("data-attrs") or ""
    if raw:
        try:
            meta = json.loads(raw)
        except json.JSONDecodeError:
            meta = {}
    src = meta.get("src") or node["attrs"].get("src") or ""
    alt = meta.get("alt") or node["attrs"].get("alt") or ""
    if not isinstance(alt, str) or alt == "null":
        alt = ""
    width = meta.get("width") or node["attrs"].get("width")
    height = meta.get("height") or node["attrs"].get("height")
    try:
        width = int(width) if width else None
        height = int(height) if height else None
    except (TypeError, ValueError):
        width = height = None
    return {"src": src, "alt": alt, "width": width, "height": height}


def is_tracking_image(meta: dict) -> bool:
    src = (meta.get("src") or "").lower()
    if "eotrx." in src or "/track" in src or "pixel" in src:
        return True
    if meta.get("width") == 1 or meta.get("height") == 1:
        return True
    return False


def heading_shift(raw_html: str) -> int:
    levels = [int(level) for level in re.findall(r"(?i)<h([1-6])\b", raw_html or "")]
    if not levels:
        return 0
    return 2 - min(levels)


def node_text(node: object) -> str:
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    return "".join(node_text(child) for child in node["children"])


def clean_href(href: str) -> str | None:
    href = (href or "").strip()
    if not href or href.startswith(("#", "javascript:", "data:")):
        return None
    if SUPERHUMAN_RE.search(href):
        return None
    parsed = urllib.parse.urlsplit(href)
    if parsed.scheme and parsed.scheme not in {"http", "https", "mailto"}:
        return None
    if parsed.scheme in {"http", "https"}:
        query = [
            (key, value)
            for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
        ]
        href = urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))
    return href


def make_figure(meta: dict, store: ImageStore, title: str, cover: bool) -> dict | None:
    if is_tracking_image(meta):
        return None
    origin = meta["src"]
    if "substack-post-media" not in origin and "substackcdn.com" not in origin and not origin.startswith("https://"):
        return None
    if "substackcdn.com/image/fetch/" in origin and "https%3A" in origin:
        encoded = origin.split("https%3A", 1)[-1]
        # The fetch URL wraps the original. Prefer data-attrs src, already handled.
    saved = store.fetch(origin, want_jpeg=cover)
    if not saved:
        return None
    alt = collapse(meta.get("alt") or "")
    if not alt:
        alt = f"Cover image for “{title}”" if cover else f"Image from the essay “{title}”"
    width = saved.get("width") or meta.get("width")
    height = saved.get("height") or meta.get("height")
    return {
        "tag": "figure",
        "attrs": {"class": "cover" if cover else ""},
        "children": [{
            "tag": "img",
            "attrs": {
                "src": saved["webp"],
                "alt": alt,
                "width": str(width) if width else "",
                "height": str(height) if height else "",
                "loading": "eager" if cover else "lazy",
                "decoding": "async",
                "fetchpriority": "high" if cover else "",
            },
            "children": [],
        }],
        "_origin": origin.split("?", 1)[0],
        "_jpg": saved.get("jpg"),
        "_remote": saved.get("remote"),
    }


def transform(node: object, store: ImageStore, title: str, shift: int, unknown: set[str]) -> list[object]:
    if isinstance(node, str):
        text = scrub_text(node)
        return [text] if text else []
    if not isinstance(node, dict):
        return []
    tag = node["tag"]
    if tag is None:
        out: list[object] = []
        for child in node["children"]:
            out.extend(transform(child, store, title, shift, unknown))
        return out
    classes = class_list(node)
    if "captioned-image-container" in classes or (tag == "figure" and find_images(node)):
        figures = []
        caption = ""
        for image in find_images(node):
            meta = image_meta(image)
            figure = make_figure(meta, store, title, cover=False)
            if figure:
                figures.append(figure)
        for child in node["children"]:
            if isinstance(child, dict) and child["tag"] == "figcaption":
                caption = collapse(node_text(child))
        if caption and figures:
            figures[0]["children"].append(caption)
        return figures
    if tag in DROP_TAGS or any(token in classes for token in ("subscription-widget", "subscribe-widget", "button-wrapper")):
        return []
    if tag == "img":
        figure = make_figure(image_meta(node), store, title, cover=False)
        return [figure] if figure else []
    if tag in HEADING_TAGS:
        level = int(tag[1]) + shift
        level = min(4, max(2, level))
        tag = f"h{level}"
    elif tag == "b":
        tag = "strong"
    elif tag == "i":
        tag = "em"
    children: list[object] = []
    for child in node["children"]:
        children.extend(transform(child, store, title, shift, unknown))
    if tag == "a":
        href = clean_href(node["attrs"].get("href", ""))
        if not href:
            return children
        link_text = collapse(node_text({"tag": "a", "attrs": {}, "children": children}))
        if not link_text or SUPERHUMAN_RE.search(link_text):
            return []
        return [{"tag": "a", "attrs": {"href": href}, "children": children}]
    if tag in UNWRAP_TAGS or tag not in KEEP_TAGS:
        if tag not in UNWRAP_TAGS | KEEP_TAGS | HEADING_TAGS | {None}:
            unknown.add(tag)
        return children
    if tag in {"h2", "h3", "h4"} and len(children) == 1 and isinstance(children[0], dict) and children[0]["tag"] in {"strong", "em"}:
        children = children[0]["children"]
    if tag in {"p", "h2", "h3", "h4", "li", "blockquote"} and not collapse(node_text({"tag": tag, "attrs": {}, "children": children})):
        # Keep the element when it holds a figure and nothing else.
        if not any(isinstance(child, dict) and child.get("tag") == "figure" for child in children):
            return []
    if tag == "p":
        plain = collapse(node_text({"tag": "p", "attrs": {}, "children": children}))
        if plain.lower() in {"share", "subscribe", "leave a comment", "restack"}:
            return []
        if SHARE_FOOTER_RE.match(plain):
            return []
    return [{"tag": tag, "attrs": {}, "children": children}]


def render_nodes(nodes: list[object]) -> str:
    parts: list[str] = []

    def render(node: object) -> None:
        if isinstance(node, str):
            parts.append(html.escape(node, quote=False))
            return
        if not isinstance(node, dict):
            return
        tag = node["tag"]
        attrs = []
        for key, value in node["attrs"].items():
            if value:
                attrs.append(f' {key}="{html.escape(str(value), quote=True)}"')
        if tag == "a":
            href = node["attrs"].get("href", "")
            parsed = urllib.parse.urlsplit(href)
            external = parsed.scheme in {"http", "https"} and "sidjashnani.com" not in (parsed.netloc or "")
            if external:
                attrs.append(' target="_blank" rel="noopener noreferrer"')
        if tag in VOID_TAGS:
            parts.append(f"<{tag}{''.join(attrs)}>")
            return
        parts.append(f"<{tag}{''.join(attrs)}>")
        for child in node["children"]:
            if isinstance(child, str) and tag == "figure":
                caption = collapse(child)
                if caption:
                    parts.append(f"<figcaption>{html.escape(caption, quote=False)}</figcaption>")
            else:
                render(child)
        if tag == "a":
            href = node["attrs"].get("href", "")
            parsed = urllib.parse.urlsplit(href)
            external = parsed.scheme in {"http", "https"} and "sidjashnani.com" not in (parsed.netloc or "")
            if external:
                parts.append('<span class="sr-only"> (opens in a new tab)</span>')
        parts.append(f"</{tag}>")

    for node in nodes:
        render(node)
    return "".join(parts)


def render_markdown(nodes: list[object]) -> str:
    lines: list[str] = []

    def text_of(node: object) -> str:
        if isinstance(node, str):
            return node
        if not isinstance(node, dict):
            return ""
        if node["tag"] == "a":
            label = collapse("".join(text_of(child) for child in node["children"]))
            href = node["attrs"].get("href", "")
            absolute = href if href.startswith("http") else f"{SITE}{href}"
            return f"[{label}]({absolute})"
        if node["tag"] == "strong":
            return f"**{collapse(''.join(text_of(child) for child in node['children']))}**"
        if node["tag"] == "em":
            return f"*{collapse(''.join(text_of(child) for child in node['children']))}*"
        if node["tag"] == "code":
            return f"`{collapse(''.join(text_of(child) for child in node['children']))}`"
        if node["tag"] == "br":
            return "\n"
        if node["tag"] == "img":
            src = node["attrs"].get("src", "")
            if src.startswith("/"):
                src = f"{SITE}{src}"
            alt = node["attrs"].get("alt", "")
            return f"![{alt}]({src})"
        return "".join(text_of(child) for child in node["children"])

    for node in nodes:
        if isinstance(node, str):
            if collapse(node):
                lines.append(collapse(node))
                lines.append("")
            continue
        tag = node["tag"]
        if tag in {"h2", "h3", "h4"}:
            level = int(tag[1])
            lines.append(f"{'#' * level} {collapse(text_of(node))}")
            lines.append("")
        elif tag == "p":
            lines.append(collapse(text_of(node)))
            lines.append("")
        elif tag == "blockquote":
            for row in collapse(text_of(node)).splitlines() or [""]:
                lines.append(f"> {row}")
            lines.append("")
        elif tag in {"ul", "ol"}:
            for index, child in enumerate((c for c in node["children"] if isinstance(c, dict) and c["tag"] == "li"), start=1):
                prefix = f"{index}." if tag == "ol" else "-"
                lines.append(f"{prefix} {collapse(text_of(child))}")
            lines.append("")
        elif tag == "figure":
            for child in node["children"]:
                if isinstance(child, dict) and child["tag"] == "img":
                    lines.append(text_of(child))
                    lines.append("")
            caption = " ".join(child for child in node["children"] if isinstance(child, str))
            if collapse(caption):
                lines.append(f"*{collapse(caption)}*")
                lines.append("")
        elif tag == "pre":
            lines.append("```")
            lines.append(node_text(node).strip("\n"))
            lines.append("```")
            lines.append("")
        elif tag == "hr":
            lines.append("---")
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def prepare_body(raw_html: str, title: str, store: ImageStore, cover_origin: str | None, unknown: set[str]) -> tuple[str, str, dict | None]:
    shift = heading_shift(raw_html)
    nodes = transform(parse_fragment(raw_html), store, title, shift, unknown)
    # Publication cover cards have an old title line set in the artwork
    # ("Certified Outgrow Advisor"). They are not shown, and a copy of that
    # same file inside the essay is dropped too. Figures Sid placed in the
    # essay stay. Open Graph uses the site card, which has the approved line.
    if cover_origin:
        cover_key = cover_origin.split("?", 1)[0]
        nodes = [
            node for node in nodes
            if not (isinstance(node, dict) and (node.get("_origin") or "").split("?", 1)[0] == cover_key)
        ]
    body_html = fix_article(render_nodes(nodes))
    markdown = fix_article(render_markdown(nodes))
    if "<h1" in body_html or "<button" in body_html or "pencraft" in body_html or "eotrx." in body_html:
        raise SyncError(f"Cleaned HTML for “{title}” still has page chrome")
    if SUPERHUMAN_RE.search(body_html) or SUPERHUMAN_RE.search(markdown):
        raise SyncError(f"A booking link survived in “{title}”")
    return body_html, markdown, None


def head_common(title: str, description: str, canonical: str, robots: str, extra: str) -> str:
    return (
        "<!DOCTYPE html>\n<html lang=\"en-GB\">\n<head>\n"
        "  <meta charset=\"UTF-8\">\n"
        "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">\n"
        "  <link rel=\"icon\" href=\"/assets/favicon/favicon.ico\" sizes=\"any\">\n"
        "  <link rel=\"icon\" href=\"/assets/favicon/favicon.svg\" type=\"image/svg+xml\">\n"
        "  <link rel=\"icon\" type=\"image/png\" sizes=\"32x32\" href=\"/assets/favicon/favicon-32x32.png\">\n"
        "  <link rel=\"icon\" type=\"image/png\" sizes=\"16x16\" href=\"/assets/favicon/favicon-16x16.png\">\n"
        "  <link rel=\"apple-touch-icon\" sizes=\"180x180\" href=\"/assets/favicon/apple-touch-icon.png\">\n"
        "  <link rel=\"manifest\" href=\"/assets/favicon/site.webmanifest\">\n"
        "  <meta name=\"theme-color\" content=\"#012A6A\">\n"
        "  <meta name=\"color-scheme\" content=\"light\">\n"
        f"  <title>{esc(title)}</title>\n"
        f"  <meta name=\"description\" content=\"{esc(description)}\">\n"
        "  <meta name=\"author\" content=\"Sid Jashnani\">\n"
        f"  <meta name=\"robots\" content=\"{robots}\">\n"
        f"  <link rel=\"canonical\" href=\"{html.escape(canonical, quote=True)}\">\n"
        f"  <link rel=\"alternate\" href=\"{SITE}/llms.txt\" type=\"text/plain\" title=\"Summary for language models\">\n"
        + extra +
        "  <link rel=\"preload\" href=\"/assets/fonts/dm-sans-latin.woff2\" as=\"font\" type=\"font/woff2\" crossorigin>\n"
        "  <link rel=\"preload\" href=\"/assets/fonts/poppins-700-normal-latin.woff2\" as=\"font\" type=\"font/woff2\" crossorigin>\n"
        "  <link rel=\"stylesheet\" href=\"/assets/fonts/fonts.css\">\n"
        "  <link rel=\"stylesheet\" href=\"/assets/blog.css\">\n"
        "  <script async src=\"https://www.googletagmanager.com/gtag/js?id=G-YF4Y5T2LR7\" data-cfasync=\"false\"></script>\n"
        "  <script data-cfasync=\"false\">\n"
        "    window.dataLayer = window.dataLayer || [];\n"
        "    function gtag(){dataLayer.push(arguments);}\n"
        "    gtag('js', new Date());\n"
        "    gtag('config', 'G-YF4Y5T2LR7');\n"
        "  </script>\n"
        "</head>\n"
    )


def chrome_nav(current: str) -> str:
    blog_current = ' aria-current="page"' if current == "blog" else ""
    return f"""<a class="skip" href="#main">Skip to main content</a>
<nav class="top" aria-label="Main navigation">
  <div class="wrap">
    <a class="brand" href="/"><img src="/assets/brand/sj-solid.svg" width="32" height="32" alt=""><span>Sid Jashnani</span></a>
    <ul class="navlinks">
      <li><a href="/#speaking">Speaking</a></li>
      <li><a href="/#about">About</a></li>
      <li><a href="/#advisory">Advisory</a></li>
      <li><a href="/#faq">FAQ</a></li>
      <li class="nav-blog"><a href="/blog/"{blog_current}>Blog</a></li>
      <li class="keep"><a class="btn btn-secondary" href="/?type=keynote#contact">Book Sid to speak</a></li>
    </ul>
  </div>
</nav>
"""


def chrome_footer() -> str:
    return """<footer>
  <div class="wrap">
    <div class="fcols">
      <div><a class="brand" href="/"><img src="/assets/brand/sj-solid.svg" width="32" height="32" alt=""><span>Sid Jashnani</span></a><p>Keynote speaker, CEO of Outgrow and Outgrow Certified Advisor. London, UK.</p></div>
      <div><h2>Work with Sid</h2><ul><li><a href="/?type=keynote#contact">Speaking</a></li><li><a href="/outgrow-advisory/">Outgrow Advisory</a></li><li><a href="/outgrow-workshop/">Outgrow Workshop</a></li><li><a href="/sales-training/">Sales training</a></li><li><a href="/revenue-growth/">Revenue growth</a></li><li><a href="/what-is-outgrow/">What is Outgrow</a></li></ul></div>
      <div><h2>Elsewhere</h2><ul><li><a href="/blog/">Blog</a></li><li><a href="https://runoutgrow.com/advisor/sid-jashnani" target="_blank" rel="noopener noreferrer">Outgrow profile<span class="sr-only"> (opens in a new tab)</span></a></li><li><a href="https://sidjashnani.substack.com" target="_blank" rel="noopener noreferrer">PREDICTABLE on Substack<span class="sr-only"> (opens in a new tab)</span></a></li><li><a href="https://www.linkedin.com/in/sidjash/" target="_blank" rel="noopener noreferrer">LinkedIn<span class="sr-only"> (opens in a new tab)</span></a></li><li><a href="/llms.txt">For AI</a></li></ul></div>
    </div>
    <p class="legal">© 2026 SID JASHNANI · LONDON, UK</p>
    <p class="ip">Outgrow and the Outgrow methodology are the intellectual property of Outgrow LLC.</p>
  </div>
</footer>
"""


def origin_line(canonical: str) -> str:
    return (
        '<p class="origin">Originally published on '
        f'<a href="{html.escape(canonical, quote=True)}" target="_blank" rel="noopener noreferrer">Substack'
        '<span class="sr-only"> (opens in a new tab)</span></a>. '
        f'<a href="{SUBSCRIBE}" target="_blank" rel="noopener noreferrer">Subscribe'
        '<span class="sr-only"> (opens in a new tab)</span></a> for the next letter.</p>'
    )


def render_post_page(post: dict, body_html: str, cover: dict | None) -> str:
    title = post["title"]
    description = post["summary"] or title
    canonical = post["canonical"]
    mirror = f"{SITE}/blog/{post['slug']}/"
    image = f"{SITE}/assets/og-image.jpg"
    image_alt = "Sid Jashnani on stage. Keynote speaker, CEO of Outgrow and Outgrow Certified Advisor."
    image_type = "image/jpeg"
    width, height = 1200, 630
    preload = ""
    if cover:
        img = cover["children"][0]["attrs"]
        src = img["src"]
        if cover.get("_jpg"):
            image = f"{SITE}{cover['_jpg']}"
            image_type = "image/jpeg"
        elif src.startswith("/"):
            image = f"{SITE}{src}"
            image_type = "image/webp"
        else:
            image = src
        image_alt = img.get("alt") or image_alt
        if img.get("width") and img.get("height"):
            width, height = int(img["width"]), int(img["height"])
        preload = (
            f'<link rel="preload" as="image" href="{html.escape(src, quote=True)}" fetchpriority="high">'
        )
    graph = {
        "@context": "https://schema.org",
        "@type": "BlogPosting",
        "headline": title,
        "description": description,
        "datePublished": post["published"],
        "dateModified": post.get("modified") or post["published"],
        "inLanguage": "en-GB",
        "image": image,
        "url": canonical,
        "mainEntityOfPage": canonical,
        "isBasedOn": canonical,
        "author": PERSON,
        "publisher": {"@id": f"{SITE}/#sid"},
        "isPartOf": {
            "@type": "Blog",
            "@id": f"{SITE}/blog/#blog",
            "name": "PREDICTABLE",
            "url": f"{SITE}/blog/",
        },
    }
    extra = (
        "\n  <link rel=\"alternate\" type=\"text/markdown\" href=\"index.md\" title=\"Markdown version\">\n"
        f"  {preload}\n"
        "  <meta property=\"og:type\" content=\"article\">\n"
        "  <meta property=\"og:site_name\" content=\"Sid Jashnani\">\n"
        f"  <meta property=\"og:url\" content=\"{html.escape(canonical, quote=True)}\">\n"
        f"  <meta property=\"og:title\" content=\"{esc(title)}\">\n"
        f"  <meta property=\"og:description\" content=\"{esc(description)}\">\n"
        f"  <meta property=\"og:image\" content=\"{html.escape(image, quote=True)}\">\n"
        f"  <meta property=\"og:image:width\" content=\"{width}\">\n"
        f"  <meta property=\"og:image:height\" content=\"{height}\">\n"
        f"  <meta property=\"og:image:alt\" content=\"{esc(image_alt)}\">\n"
        f"  <meta property=\"og:image:type\" content=\"{image_type}\">\n"
        "  <meta property=\"og:locale\" content=\"en_GB\">\n"
        f"  <meta property=\"article:published_time\" content=\"{html.escape(post['published'], quote=True)}\">\n"
        "  <meta property=\"article:author\" content=\"Sid Jashnani\">\n"
        "  <meta name=\"twitter:card\" content=\"summary_large_image\">\n"
        "  <meta name=\"twitter:site\" content=\"@siddjash\">\n"
        "  <meta name=\"twitter:creator\" content=\"@siddjash\">\n"
        f"  <meta name=\"twitter:title\" content=\"{esc(title)}\">\n"
        f"  <meta name=\"twitter:description\" content=\"{esc(description)}\">\n"
        f"  <meta name=\"twitter:image\" content=\"{html.escape(image, quote=True)}\">\n"
        f"  <meta name=\"twitter:image:alt\" content=\"{esc(image_alt)}\">\n"
        "  <script type=\"application/ld+json\">\n"
        + json_script(graph)
        + "\n  </script>"
    )
    kind = label_for(post.get("type") or "")
    kind_html = f" · {html.escape(kind)}" if kind else ""
    dek = f'<p class="dek">{esc(post["dek"])}</p>' if post.get("dek") else ""
    audio = ""
    if post.get("audio"):
        audio = (
            '<figure class="audio"><audio controls preload="none" '
            f'src="{html.escape(post["audio"], quote=True)}">'
            f'<a href="{html.escape(canonical, quote=True)}">Listen on Substack</a>'
            "</audio><figcaption>Podcast audio from the original Substack post. "
            "If it does not play here, use the link above.</figcaption></figure>"
        )
    cover_html = render_nodes([cover]) if cover else ""
    page_title = f"{title} | PREDICTABLE"
    document = (
        head_common(page_title, description, canonical, "index, follow, max-image-preview:large", extra)
        + "<body>\n"
        + chrome_nav("")
        + "<main id=\"main\">\n  <article class=\"essay\">\n"
        + f"    <p class=\"eyebrow\"><time datetime=\"{post['date']}\">{html.escape(post['display_date'])}</time>{kind_html}</p>\n"
        + f"    <h1>{esc(title)}</h1>\n"
        + "    <p class=\"byline\">Sid Jashnani, CEO of Outgrow and Outgrow Certified Advisor</p>\n"
        + f"    {dek}\n"
        + f"    {origin_line(canonical)}\n"
        + f"    {audio}\n"
        + f"    {cover_html}\n"
        + "    <div class=\"prose\">\n"
        + body_html
        + "\n    </div>\n"
        + f"    {origin_line(canonical)}\n"
        + "    <p class=\"back\"><a class=\"textlink\" href=\"/blog/\">All writing</a></p>\n"
        + "  </article>\n</main>\n"
        + chrome_footer()
        + "</body>\n</html>\n"
    )
    if document.count("<h1") != 1:
        raise SyncError(f"Expected one h1 in {post['slug']}")
    # mirror is referenced so a future template can expose it without a lint warning
    assert mirror.startswith(SITE)
    return document


def render_post_markdown(post: dict, markdown: str) -> str:
    kind = label_for(post.get("type") or "")
    lines = [
        f"# {post['title']}",
        "",
        f"- Date: {post['display_date']}",
        f"- Mirror: {SITE}/blog/{post['slug']}/",
        f"- Original: {post['canonical']}",
        "- Author: Sid Jashnani, CEO of Outgrow and Outgrow Certified Advisor",
    ]
    if kind:
        lines.append(f"- Type: {kind}")
    if post.get("dek"):
        lines.extend(["", post["dek"]])
    lines.extend(["", markdown.strip(), ""])
    return "\n".join(lines)


def render_index(posts: list[dict]) -> str:
    description = (
        "PREDICTABLE, the weekly letter by Sid Jashnani, CEO of Outgrow and "
        "Outgrow Certified Advisor. Essays on proactive selling, mirrored from Substack."
    )
    items = []
    blog_posts = []
    for post in posts:
        kind = label_for(post.get("type") or "")
        kind_html = f" · {html.escape(kind)}" if kind else ""
        summary = f"<p>{esc(post['summary'])}</p>" if post.get("summary") else ""
        items.append(
            "<li><article>"
            f'<p class="meta"><time datetime="{post["date"]}">{html.escape(post["display_date"])}</time>{kind_html}</p>'
            f'<h2><a href="/blog/{post["slug"]}/">{esc(post["title"])}</a></h2>'
            f"{summary}</article></li>"
        )
        blog_posts.append({
            "@type": "BlogPosting",
            "headline": post["title"],
            "datePublished": post["published"],
            "url": post["canonical"],
            "author": {"@id": f"{SITE}/#sid"},
        })
    graph = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "CollectionPage",
                "@id": f"{SITE}/blog/#page",
                "url": f"{SITE}/blog/",
                "name": "PREDICTABLE | Sid Jashnani",
                "description": description,
                "isPartOf": {"@id": f"{SITE}/#website"},
                "about": {"@id": f"{SITE}/#sid"},
                "inLanguage": "en-GB",
                "mainEntity": {"@id": f"{SITE}/blog/#blog"},
            },
            {
                "@type": "Blog",
                "@id": f"{SITE}/blog/#blog",
                "name": "PREDICTABLE",
                "url": f"{SITE}/blog/",
                "description": description,
                "author": PERSON,
                "publisher": {"@id": f"{SITE}/#sid"},
                "inLanguage": "en-GB",
                "blogPost": blog_posts,
            },
        ],
    }
    extra = (
        "\n  <meta property=\"og:type\" content=\"website\">\n"
        "  <meta property=\"og:site_name\" content=\"Sid Jashnani\">\n"
        f"  <meta property=\"og:url\" content=\"{SITE}/blog/\">\n"
        "  <meta property=\"og:title\" content=\"PREDICTABLE | Sid Jashnani\">\n"
        f"  <meta property=\"og:description\" content=\"{esc(description)}\">\n"
        f"  <meta property=\"og:image\" content=\"{SITE}/assets/og-image.jpg\">\n"
        "  <meta property=\"og:image:width\" content=\"1200\">\n"
        "  <meta property=\"og:image:height\" content=\"630\">\n"
        "  <meta property=\"og:image:alt\" content=\"Sid Jashnani on stage. Keynote speaker, CEO of Outgrow and Outgrow Certified Advisor.\">\n"
        "  <meta property=\"og:locale\" content=\"en_GB\">\n"
        "  <meta name=\"twitter:card\" content=\"summary_large_image\">\n"
        "  <meta name=\"twitter:site\" content=\"@siddjash\">\n"
        "  <meta name=\"twitter:title\" content=\"PREDICTABLE | Sid Jashnani\">\n"
        f"  <meta name=\"twitter:description\" content=\"{esc(description)}\">\n"
        f"  <meta name=\"twitter:image\" content=\"{SITE}/assets/og-image.jpg\">\n"
        "  <script type=\"application/ld+json\">\n"
        + json_script(graph)
        + "\n  </script>"
    )
    list_html = "\n".join(items)
    return (
        head_common("PREDICTABLE | Sid Jashnani", description, f"{SITE}/blog/", "index, follow", extra)
        + "<body>\n"
        + chrome_nav("blog")
        + "<main id=\"main\">\n"
        + "  <header class=\"essay index-head\">\n"
        + "    <p class=\"eyebrow\">PREDICTABLE</p>\n"
        + "    <h1>PREDICTABLE: essays on proactive selling by Sid Jashnani</h1>\n"
        + "    <p class=\"lead\">PREDICTABLE is the weekly letter from Sid Jashnani, CEO of Outgrow and Outgrow Certified Advisor, about proactive selling and the small daily habits that lead to organic sales growth from the customers you already have. Every essay first appeared on Substack, and you'll find a link to the original under each one.</p>\n"
        + f"    <p class=\"origin\"><a href=\"{SUBSCRIBE}\" target=\"_blank\" rel=\"noopener noreferrer\">Subscribe on Substack<span class=\"sr-only\"> (opens in a new tab)</span></a></p>\n"
        + "  </header>\n"
        + "  <div class=\"wrap index-wrap\">\n    <ol class=\"index\">\n"
        + list_html
        + "\n    </ol>\n  </div>\n</main>\n"
        + chrome_footer()
        + "</body>\n</html>\n"
    )


def render_latest(posts: list[dict]) -> str:
    cards = []
    for post in posts[:3]:
        summary = f"<p>{esc(post['summary'])}</p>" if post.get("summary") else ""
        cards.append(
            "<li>"
            f'<a href="/blog/{post["slug"]}/">'
            f'<time datetime="{post["date"]}">{html.escape(post["display_date"])}</time>'
            f"<h3>{esc(post['title'])}</h3>"
            f"{summary}</a></li>"
        )
    inner = "\n".join(cards)
    return (
        "<section class=\"band wall\" id=\"writing\" aria-labelledby=\"writing-h\">\n"
        "  <div class=\"wrap\">\n"
        "    <p class=\"eyebrow\">PREDICTABLE</p>\n"
        "    <h2 id=\"writing-h\">Latest writing</h2>\n"
        "    <ol class=\"writing\">\n"
        + inner
        + "\n    </ol>\n"
        "    <p class=\"writing-more\"><a class=\"textlink\" href=\"/blog/\">All writing</a></p>\n"
        "  </div>\n</section>"
    )


def render_llms_index(posts: list[dict]) -> str:
    lines = [
        "## Writing",
        "",
        "PREDICTABLE is mirrored at https://sidjashnani.com/blog/. "
        "Each essay was originally published on Substack, and that URL stays the original. "
        "Full text is in https://sidjashnani.com/llms-full.txt and in a Markdown file beside each essay.",
        "",
    ]
    for post in posts:
        title = post["title"].replace("|", "—")
        summary = (post.get("summary") or "").replace("|", "—")
        lines.append(
            f"- {post['date']} | {title} | {SITE}/blog/{post['slug']}/ | {summary} "
            f"Original: {post['canonical']}"
        )
    return "\n".join(lines)


def render_llms_full(posts: list[dict], markdowns: dict[str, str]) -> str:
    parts = [
        "## Writing",
        "",
        "The essays below are the text mirrored at https://sidjashnani.com/blog/. "
        "The Substack link under each title is the original. "
        "Sid Jashnani is the author: CEO of Outgrow and Outgrow Certified Advisor.",
        "",
    ]
    for post in posts:
        parts.append(f"## {post['title']}")
        parts.append("")
        parts.append(f"- Date: {post['display_date']}")
        parts.append(f"- Mirror: {SITE}/blog/{post['slug']}/")
        parts.append(f"- Original: {post['canonical']}")
        if post.get("dek"):
            parts.extend(["", post["dek"]])
        body = markdowns.get(post["slug"]) or "_The essay text is in the Markdown file beside the mirror._"
        parts.extend(["", body.strip(), ""])
    return "\n".join(parts).strip()


def sitemap_lastmod(post: dict) -> str:
    """London calendar day of the essay's last edit, else its publication day."""
    raw = post.get("modified") or post.get("published") or post["date"]
    if isinstance(raw, str) and "T" in raw:
        try:
            return london_date(raw)[0]
        except ValueError:
            return post["date"]
    if isinstance(raw, str) and len(raw) >= 10:
        return raw[:10]
    return post["date"]


def render_sitemap(posts: list[dict]) -> str:
    dates = [sitemap_lastmod(post) for post in posts]
    newest = max(dates) if dates else datetime.now(LONDON).date().isoformat()
    rows = [
        "  <url>",
        f"    <loc>{SITE}/blog/</loc>",
        f"    <lastmod>{newest}</lastmod>",
        "    <changefreq>weekly</changefreq>",
        "    <priority>0.8</priority>",
        "  </url>",
    ]
    for post, lastmod in zip(posts, dates):
        rows.extend([
            "  <url>",
            f"    <loc>{SITE}/blog/{post['slug']}/</loc>",
            f"    <lastmod>{lastmod}</lastmod>",
            "    <changefreq>monthly</changefreq>",
            "    <priority>0.6</priority>",
            "  </url>",
        ])
    return "\n".join(rows)


def load_manifest() -> dict:
    path = ROOT / "blog" / "manifest.json"
    if not path.exists():
        return {"template": 0, "posts": []}
    return json.loads(path.read_text(encoding="utf-8"))


def normalise_kept(record: dict) -> dict:
    return record


def build_record(source: dict, raw_html: str) -> dict:
    slug = (source.get("slug") or "").strip().lower()
    if not SLUG_RE.fullmatch(slug):
        raise SyncError(f"Unsafe slug {source.get('slug')!r}")
    title = collapse(source.get("title") or "")
    subtitle = source.get("subtitle") or source.get("description") or ""
    published = source.get("post_date") or source.get("published_at")
    if not title or not published:
        raise SyncError(f"{slug} is missing a title or date")
    date, display = london_date(published)
    dek = usable_subtitle(subtitle)
    summary_source = dek or first_paragraphs(raw_html)
    summary = first_sentence(summary_source)
    canonical = source.get("canonical_url") or f"https://sidjashnani.substack.com/p/{slug}"
    if not canonical.startswith("https://sidjashnani.substack.com/"):
        canonical = f"https://sidjashnani.substack.com/p/{slug}"
    audio = source.get("podcast_url") if source.get("type") == "podcast" else ""
    if audio and SUPERHUMAN_RE.search(audio):
        audio = ""
    return {
        "slug": slug,
        "title": scrub_text(title),
        "dek": dek,
        "summary": summary,
        "date": date,
        "display_date": display,
        "published": datetime.fromisoformat(published.replace("Z", "+00:00")).astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "modified": (source.get("updated_at") or published),
        "canonical": canonical,
        "type": source.get("type") or "newsletter",
        "audio": audio or "",
        "cover_origin": (source.get("cover_image") or "").split("?", 1)[0],
        "raw_html": raw_html,
    }


def public_scan(posts_html: dict[str, str], extra_texts: list[tuple[str, str]]) -> None:
    offenders = []
    for name, text in list(posts_html.items()) + extra_texts:
        if EOS_RE.search(text) or REKRUUTO_RE.search(text) or SUPERHUMAN_RE.search(text) or "Certified Outgrow" in text:
            offenders.append(name)
    if offenders:
        raise SyncError("Generated public files mention a blocked term: " + ", ".join(offenders))


def sync(refresh: bool) -> None:
    require_markers()
    previous = load_manifest()
    previous_posts = {item["slug"]: item for item in previous.get("posts", [])}
    log("Fetching the Substack archive…")
    archive = fetch_archive(refresh)
    if len(archive) < 10:
        raise SyncError(f"Archive returned only {len(archive)} posts. Leaving the mirror untouched.")
    prior_count = previous.get("archive_count") or 0
    if prior_count and len(archive) < prior_count * 0.6:
        raise SyncError(
            f"Archive shrank from {prior_count} to {len(archive)}. Leaving the mirror untouched."
        )
    log(f"Archive has {len(archive)} posts.")

    included: list[dict] = []
    skipped: list[dict] = []
    warnings: list[dict] = []
    markdowns: dict[str, str] = {}
    unknown: set[str] = set()
    seen_slugs: set[str] = set()

    for item in archive:
        slug = (item.get("slug") or "").strip().lower()
        if not slug or slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        title = collapse(item.get("title") or "")
        subtitle = collapse(item.get("subtitle") or "")
        try:
            full = fetch_post(slug, refresh)
        except Exception as error:
            log(f"KEEP  {slug}: {error}")
            if slug in previous_posts:
                included.append(previous_posts[slug])
                markdown_path = ROOT / "blog" / slug / "index.md"
                if markdown_path.exists():
                    markdowns[slug] = markdown_path.read_text(encoding="utf-8")
            else:
                warnings.append({"slug": slug, "title": title, "reason": "Could not fetch the post. Not published."})
            continue
        raw_html = full.get("body_html") or ""
        # The single-post payload is the one with the body. Titles there win.
        merged = dict(item)
        merged.update({key: value for key, value in full.items() if value not in (None, "")})
        rule_text = body_plain_for_rules(raw_html)
        reasons = exclusion_reasons(
            collapse(merged.get("title") or ""),
            collapse(merged.get("subtitle") or ""),
            rule_text,
        )
        published = merged.get("post_date") or ""
        date = ""
        if published:
            date, _display = london_date(published)
        if reasons:
            skipped.append({
                "slug": slug,
                "title": collapse(merged.get("title") or title),
                "date": date,
                "url": merged.get("canonical_url") or f"https://sidjashnani.substack.com/p/{slug}",
                "type": merged.get("type") or "",
                "reasons": reasons,
            })
            log(f"SKIP  {slug}: {'; '.join(reasons)}")
            continue
        if merged.get("type") != "podcast" and (merged.get("wordcount") or 0) >= 200 and len(rule_text.split()) < 40:
            warnings.append({"slug": slug, "title": title, "reason": "Body looked empty. Not published."})
            log(f"WARN  {slug}: body looked empty")
            if slug in previous_posts:
                included.append(previous_posts[slug])
            continue
        try:
            record = build_record(merged, raw_html)
        except SyncError as error:
            warnings.append({"slug": slug, "title": title, "reason": str(error)})
            log(f"WARN  {slug}: {error}")
            if slug in previous_posts:
                included.append(previous_posts[slug])
            continue
        store = ImageStore(slug, (previous_posts.get(slug) or {}).get("images") or {})
        try:
            body_html, markdown, cover = prepare_body(
                raw_html, record["title"], store, record["cover_origin"] or None, unknown
            )
        except SyncError as error:
            warnings.append({"slug": slug, "title": record["title"], "reason": str(error)})
            log(f"WARN  {slug}: {error}")
            if slug in previous_posts:
                included.append(previous_posts[slug])
            continue
        page = render_post_page(record, body_html, cover)
        md = render_post_markdown(record, markdown)
        public_scan({slug: page + md}, [])
        write_text(ROOT / "blog" / slug / "index.html", page)
        write_text(ROOT / "blog" / slug / "index.md", md)
        store.prune()
        record_out = {key: value for key, value in record.items() if key != "raw_html"}
        record_out["images"] = store.saved
        record_out["template"] = TEMPLATE_VERSION
        included.append(record_out)
        markdowns[slug] = md
        log(f"OK    {record['date']} {slug}")

    included.sort(key=lambda post: (post["published"], post["slug"]), reverse=True)
    skipped.sort(key=lambda post: (post.get("date") or "", post["slug"]), reverse=True)

    # Posts we no longer saw, and posts now excluded, leave the mirror.
    # Fetch failures of known posts were appended above, so they stay.
    keep_slugs = {post["slug"] for post in included}
    blog_root = ROOT / "blog"
    if blog_root.is_dir():
        for child in blog_root.iterdir():
            if child.is_dir() and child.name not in keep_slugs and child.name != "assets":
                shutil.rmtree(child)
    assets_root = ROOT / "assets" / "blog"
    if assets_root.is_dir():
        for child in assets_root.iterdir():
            if child.is_dir() and child.name not in keep_slugs:
                shutil.rmtree(child)

    index_html = render_index(included)
    public_scan({"blog-index": index_html}, [])
    write_text(blog_root / "index.html", index_html)

    latest = render_latest(included)
    index_page = (ROOT / "index.html").read_text(encoding="utf-8")
    index_page = replace_between(index_page, "<!-- sid-blog-latest:start -->", "<!-- sid-blog-latest:end -->", latest)
    write_text(ROOT / "index.html", index_page)

    # Only the marked blog block is replaced. Homepage lastmod, and the
    # Outgrow/outgrow.co note plus the current-title line in the llms files,
    # sit outside the markers and stay put.
    sitemap = (ROOT / "sitemap.xml").read_text(encoding="utf-8")
    sitemap = replace_between(sitemap, "<!-- sid-blog-sitemap:start -->", "<!-- sid-blog-sitemap:end -->", render_sitemap(included))
    write_text(ROOT / "sitemap.xml", sitemap)

    llms = (ROOT / "llms.txt").read_text(encoding="utf-8")
    llms = replace_between(llms, "<!-- sid-blog-index:start -->", "<!-- sid-blog-index:end -->", render_llms_index(included))
    write_text(ROOT / "llms.txt", llms)

    full_parts = []
    for post in included:
        if post["slug"] not in markdowns:
            path = blog_root / post["slug"] / "index.md"
            if path.exists():
                markdowns[post["slug"]] = path.read_text(encoding="utf-8")
        full_parts.append(post)
    llms_full = (ROOT / "llms-full.txt").read_text(encoding="utf-8")
    llms_full = replace_between(
        llms_full,
        "<!-- sid-blog-full:start -->",
        "<!-- sid-blog-full:end -->",
        render_llms_full(full_parts, markdowns),
    )
    public_scan({}, [("llms-full", llms_full), ("llms", llms), ("sitemap", sitemap), ("home", index_page)])
    write_text(ROOT / "llms-full.txt", llms_full)

    manifest = {
        "template": TEMPLATE_VERSION,
        "archive_count": len(archive),
        "posts": [{key: value for key, value in post.items() if key != "raw_html"} for post in included],
    }
    write_text(blog_root / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    skip_doc = {
        "note": "Essays excluded from the public mirror. This file is not linked from the site.",
        "skipped": skipped,
    }
    write_text(blog_root / "skipped.json", json.dumps(skip_doc, ensure_ascii=False, indent=2) + "\n")
    write_text(blog_root / "sync-warnings.json", json.dumps({"warnings": warnings}, ensure_ascii=False, indent=2) + "\n")

    if unknown:
        log("Unwrapped tags: " + ", ".join(sorted(unknown)))
    log(f"Published {len(included)}. Skipped {len(skipped)}. Warnings {len(warnings)}.")


def run_self_tests() -> None:
    cases = [
        ("We Run on EOS", True),
        ("EOS Implementer", True),
        ("8 Brutal EOS® Truths", True),
        ("eos", True),
        ("E.O.S. in the title", True),
        ("EO chapter night", False),
        ("eosin stain", False),
        ("geospatial data", False),
        ("the_EOS_hash", False),
        ("Neos", False),
        ("PREDICTABLE", False),
    ]
    for text, expected in cases:
        found = bool(EOS_RE.search(text))
        if found != expected:
            raise SystemExit(f"EOS check failed for {text!r}: got {found}")
    if not REKRUUTO_RE.search("We built Rekruuto"):
        raise SystemExit("Rekruuto was not detected")
    if REKRUUTO_RE.search("recruiter"):
        raise SystemExit("recruiter was treated as Rekruuto")
    if exclusion_reasons("Hello", "", "We run on EOS today") != ["Mentions EOS in the body"]:
        raise SystemExit("Body reason was wrong")
    if usable_subtitle("A full sentence."):
        pass
    else:
        raise SystemExit("A finished subtitle was dropped")
    truncated = "A moat is what protects your business from competitors, and AI has made the technical ones much easier to copy. For most businesses the real one is the relationship with customers, and it only counts"
    if usable_subtitle(truncated):
        raise SystemExit("A cut-off subtitle was kept")
    sample = (
        '<p>Book via https://superhuman.com/book/abc123 please.</p>'
        '<p>I’m a Certified Outgrow Advisor.</p>'
        '<div class="captioned-image-container"><figure><a class="image-link">'
        '<img alt="A castle moat" width="1080" height="1350" '
        'data-attrs="{&quot;src&quot;:&quot;https://substack-post-media.s3.amazonaws.com/public/images/example.png&quot;,&quot;alt&quot;:&quot;A castle moat&quot;,&quot;width&quot;:1080,&quot;height&quot;:1350}">'
        '<button type="button" class="pencraft">Restack</button></a></figure></div>'
        '<h3>Why it matters</h3>'
        '<p><a href="https://superhuman.com/book/xyz">Book a time</a> after lunch.</p>'
        '<p>Thanks for reading PREDICTABLE! This post is public so feel free to share it.</p>'
        '<h3>Why I became a <a href="https://runoutgrow.com/advisor/sid-jashnani">Certified Outgrow Sales Advisor</a></h3>'
        '<p>one of our Certified Outgrow Advisors and a Certified Outgrow Sales Advisor.</p>'
        '<iframe src="https://eotrx.substackcdn.com/open"></iframe>'
    )
    store = ImageStore("self-test", {})
    store.fetch = lambda origin, want_jpeg=False: {  # type: ignore[method-assign]
        "webp": "/assets/blog/self-test/example.webp",
        "jpg": None,
        "width": 1080,
        "height": 1350,
        "remote": False,
    }
    body, markdown, _cover = prepare_body(sample, "A test essay", store, None, set())
    if "superhuman" in body.lower() or "superhuman" in markdown.lower():
        raise SystemExit("Booking link was not stripped")
    if "Certified Outgrow Advisor" in body or "pencraft" in body or "<button" in body or "<h3" in body or "<h1" in body:
        raise SystemExit(f"Sanitiser missed something: {body}")
    if "Outgrow Certified Advisor" not in body or "<h2>" not in body or "example.webp" not in body:
        raise SystemExit(f"Sanitiser dropped content: {body}")
    if "Certified Outgrow" in body or "Thanks for reading" in body or "share it" in body:
        raise SystemExit(f"Sanitiser kept a footer or the old title: {body}")
    sitemap_xml = render_sitemap([
        {
            "slug": "example-essay",
            "date": "2026-10-01",
            "published": "2026-10-01T08:00:00Z",
            "modified": "2026-10-22T12:00:00Z",
        }
    ])
    if "<lastmod>2026-10-22</lastmod>" not in sitemap_xml or "example-essay" not in sitemap_xml:
        raise SystemExit(f"Sitemap lastmod did not follow the modified date: {sitemap_xml}")
    if "llms" in sitemap_xml:
        raise SystemExit("Sitemap renderer included an llms URL")
    if "Outgrow Certified Advisors" not in body:
        raise SystemExit("Plural title was not rewritten")
    if "became a " in body or "became a " in markdown:
        raise SystemExit(f"Article was not fixed: {body}")
    if "became an " not in body:
        raise SystemExit("Expected 'became an Outgrow Certified Advisor'")
    header = b"RIFF" + (18).to_bytes(4, "little") + b"WEBPVP8X" + (10).to_bytes(4, "little") + b"\x00\x00\x00\x00" + (1079).to_bytes(3, "little") + (1349).to_bytes(3, "little")
    if webp_size(header) != (1080, 1350):
        raise SystemExit(f"WebP size parser failed: {webp_size(header)}")
    post = {
        "slug": "a-test-essay",
        "title": "A test essay",
        "dek": "A full sentence.",
        "summary": "A full sentence.",
        "date": "2026-10-20",
        "display_date": "20 October 2026",
        "published": "2026-10-20T08:00:00Z",
        "modified": "2026-10-20T08:00:00Z",
        "canonical": "https://sidjashnani.substack.com/p/a-test-essay",
        "type": "newsletter",
        "audio": "",
    }
    page = render_post_page(post, body, None)
    if page.count("<h1") != 1 or "rel=\"canonical\" href=\"https://sidjashnani.substack.com/p/a-test-essay\"" not in page:
        raise SystemExit("Post page canonical or h1 is wrong")
    if "Originally published on" not in page or SUBSCRIBE not in page:
        raise SystemExit("Origin or subscribe link missing")
    if "BlogPosting" not in page or "Outgrow Certified Advisor" not in page:
        raise SystemExit("JSON-LD or byline missing")
    data = json.loads(re.search(r'<script type="application/ld\+json">\n(.*?)\n  </script>', page, re.S).group(1).replace("\\u0026", "&").replace("\\u003c", "<").replace("\\u003e", ">"))
    if data["@type"] != "BlogPosting" or data["author"]["name"] != "Sid Jashnani":
        raise SystemExit("JSON-LD did not parse")
    print("self-test ok")


def main() -> None:
    parser = argparse.ArgumentParser(description="Mirror the PREDICTABLE Substack to /blog/")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--refresh", action="store_true", help="Ignore the local HTTP cache")
    args = parser.parse_args()
    if args.self_test:
        run_self_tests()
        return
    try:
        sync(refresh=args.refresh)
    except SyncError as error:
        log(f"ERROR {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
