#!/usr/bin/env python3
"""Tell IndexNow about URLs that changed on sidjashnani.com.

The key file is a hex text file at the site root. Its name is the key
and its body is the same key, which is what IndexNow checks.

  python3 scripts/indexnow.py --self-test
  python3 scripts/indexnow.py --files /tmp/changed.txt
  python3 scripts/indexnow.py --range <before-sha> <after-sha>
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = "https://sidjashnani.com"
HOST = "sidjashnani.com"
KEY_NAME = re.compile(r"^[a-f0-9]{8,128}\.txt$")
LOC = re.compile(r"<loc>(https://sidjashnani.com/[^<]+)</loc>")
POST = re.compile(r"blog/([a-z0-9-]+)/index\.html")
ZERO = "0" * 40


def load_key() -> str:
    matches = []
    for path in ROOT.glob("*.txt"):
        if not KEY_NAME.fullmatch(path.name):
            continue
        body = path.read_text(encoding="utf-8").strip()
        if body == path.stem:
            matches.append(path.stem)
    if len(matches) != 1:
        raise SystemExit(f"Expected one IndexNow key file at the site root, found {matches}")
    return matches[0]


def urls_for_paths(paths: list[str], sitemap_diff: str = "") -> list[str]:
    urls: list[str] = []
    for raw in paths:
        path = raw.strip().lstrip("./")
        if not path or path.startswith(("templates/", "worker/", "assets/")):
            continue
        if path == "index.html":
            urls.append(f"{SITE}/")
        elif path == "blog/index.html":
            urls.append(f"{SITE}/blog/")
        else:
            match = POST.fullmatch(path)
            if match and match.group(1) != "skipped.json":
                urls.append(f"{SITE}/blog/{match.group(1)}/")
        if path == "sitemap.xml":
            for line in sitemap_diff.splitlines():
                if line.startswith("+") and not line.startswith("+++"):
                    found = LOC.search(line)
                    if found:
                        urls.append(found.group(1))
    seen: set[str] = set()
    unique = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def git_paths(before: str, after: str) -> tuple[list[str], str]:
    if not before or before == ZERO:
        names = subprocess.check_output(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", after or "HEAD"],
            cwd=ROOT,
            text=True,
        )
        diff = subprocess.check_output(
            ["git", "diff-tree", "--no-commit-id", "-U0", "-r", after or "HEAD", "--", "sitemap.xml"],
            cwd=ROOT,
            text=True,
        )
        return names.splitlines(), diff
    names = subprocess.check_output(
        ["git", "diff", "--name-only", f"{before}..{after}"],
        cwd=ROOT,
        text=True,
    )
    diff = subprocess.check_output(
        ["git", "diff", "-U0", f"{before}..{after}", "--", "sitemap.xml"],
        cwd=ROOT,
        text=True,
    )
    return names.splitlines(), diff


def ping(urls: list[str], key: str) -> None:
    if not urls:
        print("IndexNow: no public URLs in this change.")
        return
    payload = {
        "host": HOST,
        "key": key,
        "keyLocation": f"{SITE}/{key}.txt",
        "urlList": urls,
    }
    request = urllib.request.Request(
        "https://api.indexnow.org/indexnow",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.status
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        raise SystemExit(f"IndexNow returned {error.code}: {body}") from error
    if status not in (200, 202):
        raise SystemExit(f"IndexNow returned {status}: {body}")
    print(f"IndexNow accepted {len(urls)} URL(s) ({status}).")
    for url in urls:
        print(f"  {url}")


def self_test() -> None:
    urls = urls_for_paths(
        [
            "index.html",
            "blog/index.html",
            "blog/why-not-now/index.html",
            "templates/page.html",
            "404.html",
            "sitemap.xml",
            "assets/fonts/fonts.css",
        ],
        sitemap_diff=(
            "--- a/sitemap.xml\n+++ b/sitemap.xml\n"
            "-    <loc>https://sidjashnani.com/llms.txt</loc>\n"
            "+    <loc>https://sidjashnani.com/</loc>\n"
            "+    <loc>https://sidjashnani.com/blog/why-not-now/</loc>\n"
        ),
    )
    expected = [
        f"{SITE}/",
        f"{SITE}/blog/",
        f"{SITE}/blog/why-not-now/",
    ]
    if urls != expected:
        raise SystemExit(f"URL mapping was {urls}, expected {expected}")
    key = load_key()
    if not re.fullmatch(r"[a-f0-9]{32}", key):
        raise SystemExit("IndexNow key is not 32 hex characters")
    print("IndexNow self-test passed.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ping IndexNow with changed site URLs")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--files", type=Path, help="Newline-separated paths from git diff")
    parser.add_argument("--range", nargs=2, metavar=("BEFORE", "AFTER"))
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if args.files:
        paths = args.files.read_text(encoding="utf-8").splitlines()
        diff = ""
        if "sitemap.xml" in {path.strip() for path in paths}:
            diff = subprocess.check_output(
                ["git", "diff", "-U0", "HEAD", "--", "sitemap.xml"],
                cwd=ROOT,
                text=True,
            )
            # A just-made commit has no unstaged diff. Fall back to HEAD~1.
            if "<loc>" not in diff:
                diff = subprocess.check_output(
                    ["git", "diff", "-U0", "HEAD~1", "HEAD", "--", "sitemap.xml"],
                    cwd=ROOT,
                    text=True,
                )
        urls = urls_for_paths(paths, diff)
    elif args.range:
        paths, diff = git_paths(args.range[0], args.range[1])
        urls = urls_for_paths(paths, diff)
    else:
        parser.error("Pass --files, --range, or --self-test")
        return
    ping(urls, load_key())


if __name__ == "__main__":
    main()
