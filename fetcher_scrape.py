"""
Frank — Scrape-Fetcher für Branchenradar
Ruft alle Scrape-Endpunkte aus sources.json ab und erkennt neue Artikel-Links.
"""
import json
import os
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    requests = None
    BeautifulSoup = None

DATA_DIR     = os.path.join(os.path.dirname(__file__), "data")
SOURCES_FILE = os.path.join(DATA_DIR, "sources.json")
STATE_FILE   = os.path.join(DATA_DIR, "fetcher_scrape_state.json")
OUTPUT_FILE  = os.path.join(DATA_DIR, "fetcher_scrape_latest.json")

HEADERS = {"User-Agent": "Branchenradar-Frank/1.0"}
TIMEOUT = 15


def _load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _is_article_link(href, base_url):
    """Heuristic: internal link with path depth >= 2 and no file extension noise."""
    try:
        parsed = urlparse(urljoin(base_url, href))
        base   = urlparse(base_url)
        if parsed.netloc and parsed.netloc != base.netloc:
            return False
        path = parsed.path.rstrip("/")
        if not path or path == base.path.rstrip("/"):
            return False
        # skip common non-article paths
        skip = ("/tag/", "/kategorie/", "/category/", "/author/", "/autoren/",
                "/page/", "/seite/", "#", "javascript:", "mailto:", "tel:")
        if any(s in (parsed.path + (parsed.fragment or "")).lower() for s in skip):
            return False
        # must have at least one path segment beyond the base
        segments = [s for s in path.split("/") if s]
        return len(segments) >= 1
    except Exception:
        return False


def _extract_links(html, base_url, selector=None):
    soup = BeautifulSoup(html, "html.parser")
    if selector:
        anchors = soup.select(selector)
    else:
        anchors = soup.find_all("a", href=True)

    links = []
    seen  = set()
    for a in anchors:
        href = a.get("href", "").strip()
        if not href:
            continue
        full = urljoin(base_url, href).split("#")[0].rstrip("/")
        if full in seen:
            continue
        if selector or _is_article_link(href, base_url):
            seen.add(full)
            title = a.get_text(strip=True)[:200] or full
            links.append({"url": full, "title": title})
    return links


def run():
    if requests is None or BeautifulSoup is None:
        raise RuntimeError(
            "requests und beautifulsoup4 nicht installiert. "
            "Bitte: pip install requests beautifulsoup4"
        )

    with open(SOURCES_FILE, encoding="utf-8") as f:
        sources = json.load(f)

    state  = _load_state()
    run_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    receipt    = []
    candidates = []

    scrape_sources = [
        s for s in sources
        if any(e.get("agent") == "scrape" and e.get("url") for e in s.get("endpoints", []))
    ]

    for source in scrape_sources:
        scrape_eps = [
            e for e in source.get("endpoints", [])
            if e.get("agent") == "scrape" and e.get("url")
        ]
        for ep in scrape_eps:
            url      = ep["url"]
            label    = ep.get("label", "")
            selector = ep.get("selector", "").strip() or None
            src_state = state.get(url, {"known_urls": [], "last_fetched_at": None})

            try:
                resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
                resp.raise_for_status()
                links = _extract_links(resp.text, url, selector)

                known  = set(src_state["known_urls"])
                new_links = [l for l in links if l["url"] not in known]

                for l in new_links:
                    known.add(l["url"])
                src_state["known_urls"]      = list(known)[-500:]
                src_state["last_fetched_at"] = run_at
                state[url] = src_state

                receipt.append({
                    "kuerzel":     source.get("kuerzel", ""),
                    "source_name": source.get("name", ""),
                    "url":         url,
                    "label":       label,
                    "selector":    selector or "(heuristik)",
                    "status":      "ok",
                    "new_entries": len(new_links),
                    "total":       len(links),
                })

                for l in new_links:
                    candidates.append({
                        "kuerzel":     source.get("kuerzel", ""),
                        "source_name": source.get("name", ""),
                        "title":       l["title"],
                        "url":         l["url"],
                        "fetched_at":  run_at,
                    })

            except Exception as exc:
                receipt.append({
                    "kuerzel":     source.get("kuerzel", ""),
                    "source_name": source.get("name", ""),
                    "url":         url,
                    "label":       label,
                    "selector":    selector or "(heuristik)",
                    "status":      f"fehler: {str(exc)[:120]}",
                    "new_entries": 0,
                    "total":       0,
                })

    _save_state(state)

    result = {
        "run_at":            run_at,
        "sources_checked":   len(receipt),
        "sources_ok":        sum(1 for r in receipt if r["status"] == "ok"),
        "sources_error":     sum(1 for r in receipt if r["status"] != "ok"),
        "new_entries_total": len(candidates),
        "crawl_receipt":     receipt,
        "candidates":        candidates,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    return result


def export_text(data):
    """Formatiert die Fetcher-Ergebnisse als lesbaren Text für Finn."""
    lines = [
        "# Frank-Export — Scrape-Crawler",
        f"Ausgeführt: {data['run_at']}",
        f"Neue Einträge: {data['new_entries_total']} | "
        f"Quellen: {data['sources_checked']} gecheckt, "
        f"{data['sources_ok']} OK, {data['sources_error']} Fehler",
        "",
        "---",
        "",
        "## Neue Links",
        "",
    ]

    if not data["candidates"]:
        lines.append("_(keine neuen Links gefunden)_")
        lines.append("")
    else:
        for c in data["candidates"]:
            lines.append(f"### [{c['kuerzel']}] {c['title']}")
            lines.append(f"Quelle: {c['source_name']}")
            lines.append(f"URL: {c['url']}")
            lines.append("")

    lines += [
        "---",
        "",
        "## Crawl-Receipt",
        "",
        "Kürzel | Quelle | Selektor | Status | Neu",
        "--- | --- | --- | --- | ---",
    ]
    for r in data["crawl_receipt"]:
        icon = "✅" if r["status"] == "ok" else "❌"
        lines.append(
            f"{r.get('kuerzel','')} | {r['source_name']} | "
            f"{r.get('selector','—')} | {icon} {r['status']} | {r['new_entries']}"
        )

    return "\n".join(lines)


if __name__ == "__main__":
    result = run()
    print(f"Frank fertig: {result['new_entries_total']} neue Links, "
          f"{result['sources_ok']}/{result['sources_checked']} Quellen OK")
