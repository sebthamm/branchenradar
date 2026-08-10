"""
Fletcher — Feed-Fetcher für Branchenradar
Ruft alle RSS/Atom-Feeds aus sources.json ab und speichert neue Einträge.
"""
import json
import os
import re
from datetime import datetime

try:
    import feedparser
except ImportError:
    feedparser = None

DATA_DIR    = os.path.join(os.path.dirname(__file__), "data")
SOURCES_FILE = os.path.join(DATA_DIR, "sources.json")
STATE_FILE   = os.path.join(DATA_DIR, "fetcher_feed_state.json")
OUTPUT_FILE  = os.path.join(DATA_DIR, "fetcher_feed_latest.json")


def _strip_html(text):
    return re.sub(r"<[^>]+>", " ", text or "").strip()


def _load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def run():
    if feedparser is None:
        raise RuntimeError("feedparser nicht installiert. Bitte: pip install feedparser")

    with open(SOURCES_FILE, encoding="utf-8") as f:
        sources = json.load(f)

    state    = _load_state()
    run_at   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    receipt  = []
    candidates = []

    feed_sources = [
        s for s in sources
        if any(e.get("agent") == "feed" and e.get("url") for e in s.get("endpoints", []))
    ]

    for source in feed_sources:
        feed_eps = [
            e for e in source.get("endpoints", [])
            if e.get("agent") == "feed" and e.get("url")
        ]
        for ep in feed_eps:
            url   = ep["url"]
            label = ep.get("label", "")
            src_state = state.get(url, {"known_ids": [], "last_fetched_at": None})

            try:
                feed = feedparser.parse(
                    url,
                    request_headers={"User-Agent": "Branchenradar-Fletcher/1.0"}
                )
                if feed.bozo and not feed.entries:
                    raise Exception(str(feed.bozo_exception)[:120])

                new_entries = []
                for entry in feed.entries:
                    eid = entry.get("id") or entry.get("link", "")
                    if eid and eid not in src_state["known_ids"]:
                        new_entries.append(entry)

                known = src_state["known_ids"]
                for e in new_entries:
                    eid = e.get("id") or e.get("link", "")
                    if eid:
                        known.append(eid)
                src_state["known_ids"]       = list(dict.fromkeys(known))[-500:]
                src_state["last_fetched_at"] = run_at
                state[url] = src_state

                receipt.append({
                    "kuerzel":     source.get("kuerzel", ""),
                    "source_name": source.get("name", ""),
                    "url":         url,
                    "label":       label,
                    "status":      "ok",
                    "new_entries": len(new_entries),
                    "total":       len(feed.entries),
                })

                for entry in new_entries:
                    pub = (
                        entry.get("published")
                        or entry.get("updated")
                        or ""
                    )
                    raw = (
                        entry.get("summary")
                        or (entry.content[0].get("value") if entry.get("content") else "")
                        or ""
                    )
                    candidates.append({
                        "kuerzel":     source.get("kuerzel", ""),
                        "source_name": source.get("name", ""),
                        "title":       (entry.get("title") or "").strip(),
                        "url":         entry.get("link", ""),
                        "published_at": pub,
                        "text":        _strip_html(raw)[:600],
                        "fetched_at":  run_at,
                    })

            except Exception as exc:
                receipt.append({
                    "kuerzel":     source.get("kuerzel", ""),
                    "source_name": source.get("name", ""),
                    "url":         url,
                    "label":       label,
                    "status":      f"fehler: {exc}",
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
    """Formatiert die Fetcher-Ergebnisse als lesbaren Text für Lars."""
    lines = [
        "# Fletcher-Export — Feed-Crawler",
        f"Ausgeführt: {data['run_at']}",
        f"Neue Einträge: {data['new_entries_total']} | "
        f"Quellen: {data['sources_checked']} gecheckt, "
        f"{data['sources_ok']} OK, {data['sources_error']} Fehler",
        "",
        "---",
        "",
        "## Neue Einträge",
        "",
    ]

    if not data["candidates"]:
        lines.append("_(keine neuen Einträge gefunden)_")
        lines.append("")
    else:
        for c in data["candidates"]:
            lines.append(f"### [{c['kuerzel']}] {c['title']}")
            lines.append(f"Quelle: {c['source_name']}")
            lines.append(f"URL: {c['url']}")
            if c["published_at"]:
                lines.append(f"Datum: {c['published_at']}")
            if c["text"]:
                lines.append(f"Text: {c['text']}")
            lines.append("")

    lines += [
        "---",
        "",
        "## Crawl-Receipt",
        "",
        "Kürzel | Quelle | Status | Neu",
        "--- | --- | --- | ---",
    ]
    for r in data["crawl_receipt"]:
        icon   = "✅" if r["status"] == "ok" else "❌"
        lines.append(
            f"{r.get('kuerzel','')} | {r['source_name']} | {icon} {r['status']} | {r['new_entries']}"
        )

    return "\n".join(lines)


if __name__ == "__main__":
    result = run()
    print(f"Fletcher fertig: {result['new_entries_total']} neue Einträge, "
          f"{result['sources_ok']}/{result['sources_checked']} Quellen OK")
