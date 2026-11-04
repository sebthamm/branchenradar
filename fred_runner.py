#!/usr/bin/env python3
"""
fred_runner.py — Fred: Feed-KI-Analyse-Agent für den Branchenradar.

Liest neue Feed-Kandidaten aus fetcher_feed_latest.json (von Fletcher),
bewertet sie per Claude-API und schreibt relevante Einträge als Signale
in signals.json.
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

BASE_DIR           = Path("/opt/branchenradar")
DATA_DIR           = BASE_DIR / "data"
FEED_LATEST_FILE   = DATA_DIR / "fetcher_feed_latest.json"
SOURCES_FILE       = DATA_DIR / "sources.json"
SIGNALS_FILE       = DATA_DIR / "signals.json"
SIGNALS_LOCK       = DATA_DIR / "signals.json.lock"
HISTORY_FILE       = DATA_DIR / "fred_history.json"
STATUS_FILE        = DATA_DIR / "fred_status.json"
AGENT_CONFIGS      = DATA_DIR / "agent_configs.json"

HISTORY_MAX = 50
BATCH_SIZE  = 8   # Kandidaten pro API-Aufruf (haben jeweils Text)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [fred] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("fred_runner")


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


def _source_context(kuerzel, sources_by_kuerzel):
    src = sources_by_kuerzel.get(kuerzel, {})
    parts = []
    if src.get("primary_category"):
        parts.append(src["primary_category"])
    if src.get("relevant_roles"):
        parts.append("Rollen: " + ", ".join(src["relevant_roles"]))
    return " | ".join(parts) if parts else ""


def _parse_signals(text):
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return []
    try:
        raw = json.loads(match.group(0))
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
            "source":     item.get("source") or "",
            "url":        item.get("url") or "",
            "agent":      "feed",
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
    fred_cfg = raw_cfg.get("agents", {}).get("feed", {})

    persona = fred_cfg.get("persona", "Du bist Fred, Feed-KI-Analyse-Agent des Branchenradars für das Gesundheitswesen.")
    method  = fred_cfg.get("method", "")
    hint    = fred_cfg.get("hint", "")
    model   = fred_cfg.get("model") or "claude-haiku-4-5-20251001"

    feed_data  = _load_json(FEED_LATEST_FILE, {})
    candidates = feed_data.get("candidates", [])
    sources    = _load_json(SOURCES_FILE, [])
    sources_by_kuerzel = {s.get("kuerzel", ""): s for s in sources}

    log.info("Starte Fred-Lauf: %d Feed-Kandidaten, Modell %s", len(candidates), model)

    _empty_entry = {
        "run_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "model": model, "candidates_in": 0, "signals_new": 0,
        "input_tokens": 0, "output_tokens": 0, "duration_s": 0, "cost_usd": 0,
    }
    if not candidates:
        log.info("Keine Feed-Kandidaten — Lauf wird übersprungen")
        if not dry_run:
            history = _load_json(HISTORY_FILE, [])
            history.insert(0, {**_empty_entry, "note": "Keine Kandidaten"})
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history[:HISTORY_MAX], f, ensure_ascii=False, indent=2)
            with open(STATUS_FILE, "w", encoding="utf-8") as f:
                json.dump(_empty_entry, f, ensure_ascii=False, indent=2)
        return _empty_entry

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

    batches = [candidates[i:i+BATCH_SIZE] for i in range(0, len(candidates), BATCH_SIZE)]

    for batch_idx, batch in enumerate(batches):
        items_block = ""
        for i, c in enumerate(batch, 1):
            ctx = _source_context(c.get("kuerzel", ""), sources_by_kuerzel)
            items_block += (
                f"### Kandidat {i}\n"
                f"Kürzel: {c.get('kuerzel','')} | Quelle: {c.get('source_name','')} "
                + (f"| {ctx}" if ctx else "") + "\n"
                f"Titel: {c.get('title','')}\n"
                f"URL: {c.get('url','')}\n"
                f"Veröffentlicht: {c.get('published_at','')}\n"
            )
            if c.get("text"):
                text_snippet = c["text"][:800].strip()
                items_block += f"Text: {text_snippet}\n"
            items_block += "\n"

        user_message = (
            f"## Neue Feed-Einträge ({len(batch)} Kandidaten)\n\n"
            f"{items_block}\n"
            f"Bewerte diese Feed-Einträge und extrahiere daraus Signale für den Branchenradar "
            f"Gesundheitswesen (Regulierung, Markt, Versorgung, Vergütung, Technologie, Personal).\n\n"
            f"Antworte NUR mit einem JSON-Array — nur relevante Einträge aufnehmen:\n"
            f'[{{"title":"Titel (max 80 Zeichen)","summary":"2–3 Sätze","category":"Regulierung|Markt|Technologie|Personal|Sonstiges","tags":["tag1"],"source":"KÜRZEL","url":"URL","date":"YYYY-MM-DD"}}]\n\n'
            f"Maximal 2 Signale pro Kandidat. Nicht relevante Einträge weglassen. "
            f"Wenn gar nichts relevant: []"
        )

        try:
            msg = client.messages.create(
                model=model, max_tokens=2048,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )
            in_tokens_total  += msg.usage.input_tokens  if msg.usage else 0
            out_tokens_total += msg.usage.output_tokens if msg.usage else 0
            response_text = "".join(b.text for b in msg.content if b.type == "text")
            new_sigs = _parse_signals(response_text)
            all_new_signals.extend(new_sigs)
            log.info("Batch %d/%d: %d Signale", batch_idx + 1, len(batches), len(new_sigs))
        except Exception as e:
            errors.append(f"Batch {batch_idx + 1}: {str(e)[:80]}")
            log.error("API-Fehler Batch %d: %s", batch_idx + 1, e)

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
        "signals_new":     len(all_new_signals),
        "input_tokens":    in_tokens_total,
        "output_tokens":   out_tokens_total,
        "duration_s":      duration_s,
        "cost_usd":        cost_usd,
        "errors":          errors,
        "note":            f"{'DRY RUN — ' if dry_run else ''}{len(candidates)} Kandidaten → {len(all_new_signals)} Signale",
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
    parser = argparse.ArgumentParser(description="Fred Feed-KI-Analyse-Agent")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        result = run(dry_run=args.dry_run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        log.error("Lauf fehlgeschlagen: %s", e)
        sys.exit(1)
