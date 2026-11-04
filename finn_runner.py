#!/usr/bin/env python3
"""
finn_runner.py — Finn: Scrape-KI-Analyse-Agent für den Branchenradar.

Liest neue Scrape-Kandidaten aus fetcher_scrape_latest.json (von Frank),
gruppiert sie nach Quelle, bewertet sie per Claude-API (nur Titel + URL,
kein Volltext) und schreibt relevante Einträge als Signale in signals.json.
"""

import fcntl
import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime, date
from pathlib import Path

BASE_DIR             = Path("/opt/branchenradar")
DATA_DIR             = BASE_DIR / "data"
SCRAPE_LATEST_FILE   = DATA_DIR / "fetcher_scrape_latest.json"
SOURCES_FILE         = DATA_DIR / "sources.json"
SIGNALS_FILE         = DATA_DIR / "signals.json"
SIGNALS_LOCK         = DATA_DIR / "signals.json.lock"
HISTORY_FILE         = DATA_DIR / "finn_history.json"
STATUS_FILE          = DATA_DIR / "finn_status.json"
AGENT_CONFIGS        = DATA_DIR / "agent_configs.json"

HISTORY_MAX       = 50
TITLES_PER_SOURCE = 30   # Titel je Quelle pro API-Aufruf
MAX_SIGNALS_SRC   = 3    # Maximal-Signale je Quelle

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [finn] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("finn_runner")


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json_locked(path, lock_path, data):
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            tmp = str(path) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _source_context(src):
    parts = []
    if src.get("primary_category"):
        parts.append(src["primary_category"])
    if src.get("relevant_roles"):
        parts.append("Rollen: " + ", ".join(src["relevant_roles"][:3]))
    hint = next(
        (h.get("text", "") for h in src.get("agent_hints", []) if h.get("agent") == "scrape"),
        ""
    )
    if hint:
        parts.append(f"Hinweis: {hint[:100]}")
    return " | ".join(parts) if parts else ""


def _parse_signals(text, default_source):
    text = re.sub(r"```(?:json)?\s*", "", text)
    start = text.find("[")
    end   = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        raw = json.loads(text[start:end+1])
    except Exception:
        return []
    today = date.today().isoformat()
    signals = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or item.get("titel") or "").strip()
        if not title:
            continue
        signals.append({
            "id":         str(uuid.uuid4()),
            "title":      title,
            "summary":    (item.get("summary") or item.get("zusammenfassung") or "").strip(),
            "category":   (item.get("category") or item.get("kategorie") or "Sonstiges").strip(),
            "tags":       item.get("tags", []),
            "date":       item.get("date") or today,
            "source":     item.get("source") or default_source,
            "url":        item.get("url") or "",
            "agent":      "scrape",
            "status":     "neu",
            "created_at": today,
        })
    return signals


def run(dry_run=False):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY nicht gesetzt")

    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic-Paket nicht installiert")

    raw_cfg  = _load_json(AGENT_CONFIGS, {})
    finn_cfg = raw_cfg.get("agents", {}).get("scrape", {})

    persona = finn_cfg.get("persona", "Du bist Finn, Scrape-KI-Analyse-Agent des Branchenradars für das Gesundheitswesen.")
    method  = finn_cfg.get("method", "")
    hint    = finn_cfg.get("hint", "")
    model   = finn_cfg.get("model") or "claude-haiku-4-5-20251001"

    scrape_data = _load_json(SCRAPE_LATEST_FILE, {})
    candidates  = scrape_data.get("candidates", [])
    sources     = _load_json(SOURCES_FILE, [])
    sources_by_kuerzel = {s.get("kuerzel", ""): s for s in sources}

    log.info("Starte Finn-Lauf: %d Scrape-Kandidaten, Modell %s", len(candidates), model)

    _empty_entry = {
        "run_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "model": model, "candidates_in": 0, "sources_checked": 0,
        "signals_new": 0, "input_tokens": 0, "output_tokens": 0,
        "duration_s": 0, "cost_usd": 0,
    }
    if not candidates:
        log.info("Keine Scrape-Kandidaten — Lauf wird übersprungen")
        if not dry_run:
            history = _load_json(HISTORY_FILE, [])
            history.insert(0, {**_empty_entry, "note": "Keine Kandidaten"})
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history[:HISTORY_MAX], f, ensure_ascii=False, indent=2)
            with open(STATUS_FILE, "w", encoding="utf-8") as f:
                json.dump(_empty_entry, f, ensure_ascii=False, indent=2)
        return _empty_entry

    # Kandidaten nach Kürzel gruppieren
    by_source = {}
    for c in candidates:
        k = c.get("kuerzel", "_unbekannt")
        by_source.setdefault(k, []).append(c)

    client = anthropic.Anthropic(api_key=api_key)
    t_start = time.time()

    all_new_signals  = []
    in_tokens_total  = 0
    out_tokens_total = 0
    errors           = []

    system_prompt = "\n\n".join(filter(None, [
        persona,
        f"## Methode\n{method}" if method else "",
        f"## Hinweise\n{hint}" if hint else "",
    ]))

    sources_checked = 0
    for kuerzel, items in by_source.items():
        src = sources_by_kuerzel.get(kuerzel, {})
        ctx = _source_context(src)
        source_name = src.get("name", kuerzel)

        # In Scheiben à TITLES_PER_SOURCE aufteilen
        slices = [items[i:i+TITLES_PER_SOURCE] for i in range(0, len(items), TITLES_PER_SOURCE)]

        for slice_idx, sl in enumerate(slices):
            titles_block = "\n".join(
                f"- {c.get('title', '(kein Titel)')} [{c.get('url','')}]"
                for c in sl
            )

            user_message = (
                f"## Quelle: {source_name} (Kürzel: {kuerzel})\n"
                + (f"Kontext: {ctx}\n" if ctx else "")
                + f"\n### Neue Seiten/Dokumente ({len(sl)} Einträge)\n"
                f"{titles_block}\n\n"
                f"Bewerte diese Titel und URLs: Welche enthalten relevante Signale für den Branchenradar "
                f"Gesundheitswesen (Regulierung, Markt, Versorgung, Vergütung, Technologie, Personal)?\n\n"
                f"Antworte NUR mit einem JSON-Array der relevanten Signale:\n"
                f'[{{"title":"Aussagekräftiger Titel (max 80 Zeichen)","summary":"Was ist das Signal? (1–2 Sätze, aus Titel/URL ableitbar)","category":"Regulierung|Markt|Technologie|Personal|Sonstiges","tags":["tag1"],"source":"{kuerzel}","url":"URL des Eintrags","date":"{date.today().isoformat()}"}}]\n\n'
                f"Maximal {MAX_SIGNALS_SRC} Signale. Generische Seiten (Impressum, Datenschutz, Login, AGB) ignorieren. "
                f"Wenn nichts relevant: []"
            )

            try:
                msg = client.messages.create(
                    model=model, max_tokens=1500,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_message}],
                )
                in_tokens_total  += msg.usage.input_tokens  if msg.usage else 0
                out_tokens_total += msg.usage.output_tokens if msg.usage else 0
                response_text = "".join(b.text for b in msg.content if b.type == "text")
                new_sigs = _parse_signals(response_text, kuerzel)
                all_new_signals.extend(new_sigs)
                if new_sigs:
                    log.info("%s Scheibe %d: %d Signale", kuerzel, slice_idx + 1, len(new_sigs))
            except Exception as e:
                errors.append(f"{kuerzel} Scheibe {slice_idx + 1}: {str(e)[:80]}")
                log.error("API-Fehler %s: %s", kuerzel, e)

        sources_checked += 1

    duration_s = round(time.time() - t_start, 1)
    p_in, p_out = 1.0, 5.0  # Haiku-Preise
    cost_usd = round(in_tokens_total / 1_000_000 * p_in + out_tokens_total / 1_000_000 * p_out, 4)

    if all_new_signals and not dry_run:
        existing = _load_json(SIGNALS_FILE, [])
        existing.extend(all_new_signals)
        _save_json_locked(SIGNALS_FILE, SIGNALS_LOCK, existing)
        log.info("signals.json: +%d Signale", len(all_new_signals))
    elif dry_run:
        log.info("DRY RUN: %d Signale würden geschrieben", len(all_new_signals))

    entry = {
        "run_at":          datetime.now().strftime("%Y-%m-%d %H:%M"),
        "model":           model,
        "candidates_in":   len(candidates),
        "sources_checked": sources_checked,
        "signals_new":     len(all_new_signals),
        "input_tokens":    in_tokens_total,
        "output_tokens":   out_tokens_total,
        "duration_s":      duration_s,
        "cost_usd":        cost_usd,
        "errors":          errors,
        "note":            f"{'DRY RUN — ' if dry_run else ''}{len(candidates)} Kandidaten aus {sources_checked} Quellen → {len(all_new_signals)} Signale",
    }

    if not dry_run:
        history = _load_json(HISTORY_FILE, [])
        history.insert(0, entry)
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history[:HISTORY_MAX], f, ensure_ascii=False, indent=2)
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(entry, f, ensure_ascii=False, indent=2)

    return entry


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Finn Scrape-KI-Analyse-Agent")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        result = run(dry_run=args.dry_run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        log.error("Lauf fehlgeschlagen: %s", e)
        sys.exit(1)
