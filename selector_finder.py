"""
Sol – Selektor-Finder
Besucht Scrape-Endpunkte ohne CSS-Selektor, analysiert die HTML-Struktur
und trägt automatisch den besten Kandidaten in sources.json ein.
"""
import json
import os
import re
from datetime import datetime
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup

DATA_DIR    = os.path.join(os.path.dirname(__file__), "data")
SOURCES_FILE = os.path.join(DATA_DIR, "sources.json")
STATUS_FILE  = os.path.join(DATA_DIR, "selector_finder_status.json")

_BLOCKED_TERMS = {"nav", "menu", "footer", "header", "sidebar", "breadcrumb",
                  "social", "share", "cookie", "banner", "ad", "ads", "login",
                  "search", "pagination", "pager", "lang", "meta"}

def _load_sources():
    with open(SOURCES_FILE, encoding="utf-8") as f:
        return json.load(f)

def _save_sources(sources):
    with open(SOURCES_FILE, "w", encoding="utf-8") as f:
        json.dump(sources, f, ensure_ascii=False, indent=2)

def _looks_like_article_link(a, page_url):
    href = a.get("href", "")
    if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
        return False
    text = a.get_text(strip=True)
    if len(text) < 18:
        return False
    abs_url = urljoin(page_url, href)
    parsed  = urlparse(abs_url)
    base    = urlparse(page_url)
    if parsed.netloc and parsed.netloc != base.netloc:
        return False
    if parsed.path == base.path:
        return False
    return True

def _selector_blocked(sel):
    low = sel.lower()
    return any(t in low for t in _BLOCKED_TERMS)

def _build_candidate_selectors(a):
    """Return 2–3 candidate CSS selectors for a given <a> tag."""
    candidates = []
    parent = a.parent
    if not parent or parent.name in ("html", "body", "[document]", None):
        return ["a"]

    # Pattern 1: direct parent with class
    cls = (parent.get("class") or [])
    if cls:
        candidates.append(f"{parent.name}.{cls[0]} a")

    # Pattern 2: grandparent with class
    gp = parent.parent
    if gp and gp.name not in ("html", "body", "[document]", None):
        gcls = (gp.get("class") or [])
        if gcls:
            candidates.append(f"{gp.name}.{gcls[0]} a")

    # Pattern 3: semantic tag (article, li, h2, h3)
    if parent.name in ("article", "li", "h2", "h3", "h4", "dt"):
        candidates.append(f"{parent.name} a")
    elif gp and gp.name in ("article", "li", "ul", "ol"):
        candidates.append(f"{gp.name} a")

    return candidates or ["a"]

def _detect_selector(html, page_url, min_links=3):
    soup = BeautifulSoup(html, "html.parser")
    article_links = [
        a for a in soup.find_all("a", href=True)
        if _looks_like_article_link(a, page_url)
    ]
    if len(article_links) < min_links:
        return None, len(article_links)

    counts = {}
    for a in article_links:
        for sel in _build_candidate_selectors(a):
            counts[sel] = counts.get(sel, 0) + 1

    # Filter: must meet min_links threshold, must not be navigation/footer
    valid = {
        sel: cnt for sel, cnt in counts.items()
        if cnt >= min_links and not _selector_blocked(sel) and sel != "a"
    }
    if not valid:
        return None, len(article_links)

    best = max(valid.items(), key=lambda x: x[1])
    return best[0], best[1]

def run(timeout=8, min_links=3, max_per_run=30):
    sources = _load_sources()
    run_at  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Collect scrape endpoints without selector
    todo = []
    for src in sources:
        for ep in src.get("endpoints", []):
            if ep.get("agent") == "scrape" and ep.get("url") and not ep.get("selector"):
                todo.append((src["id"], ep))

    todo = todo[:max_per_run]

    results = []
    updated = 0
    failed  = 0

    session = requests.Session()
    session.headers["User-Agent"] = "Branchenradar-Sol/1.0"

    for src_id, ep in todo:
        url = ep["url"]
        result = {"url": url, "source_id": src_id, "status": None, "selector": None, "link_count": 0}
        try:
            resp = session.get(url, timeout=timeout, allow_redirects=True)
            resp.raise_for_status()
            sel, link_count = _detect_selector(resp.text, url, min_links=min_links)
            result["link_count"] = link_count
            if sel:
                result["status"]   = "ok"
                result["selector"] = sel
                # Write selector back into source
                for src in sources:
                    if src["id"] == src_id:
                        for e in src.get("endpoints", []):
                            if e.get("url") == url and e.get("agent") == "scrape":
                                e["selector"] = sel
                updated += 1
            else:
                result["status"] = "no_selector"
                failed += 1
        except Exception as ex:
            result["status"] = "error"
            result["error"]  = str(ex)[:120]
            failed += 1
        results.append(result)

    _save_sources(sources)

    status = {
        "run_at":        run_at,
        "endpoints_checked": len(todo),
        "updated":       updated,
        "failed":        failed,
        "skipped_total": len([s for src in sources for e in src.get("endpoints", [])
                               if e.get("agent") == "scrape" and e.get("selector")]),
        "remaining":     max(0, sum(
            1 for src in sources for e in src.get("endpoints", [])
            if e.get("agent") == "scrape" and e.get("url") and not e.get("selector")
        )),
        "results": results,
    }
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)

    return status
