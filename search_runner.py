#!/usr/bin/env python3
"""
search_runner.py — Lena: Such-/Login-Quellen-Analyse-Agent für den Branchenradar.

Läuft als Standalone-Script (wird von APScheduler per _lena_scheduled_job aufgerufen)
oder direkt: python3 search_runner.py [--dry-run]

Ablauf:
1. Quellen mit Ingestion-Methode "search" laden
2. Für jede Quelle: Metadaten + Hinweise sammeln, ggf. öffentlich zugängliche URL analysieren
3. Prompt aufbauen (Persona + Methode + Hint + Quellenbatch)
4. Anthropic API aufrufen (claude-opus-4-8)
5. Signale aus der Antwort parsen
6. Neue Signale in signals.json schreiben (mit Lock)
7. Lauf-Eintrag in lena_history.json schreiben
"""

import fcntl
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, date
from pathlib import Path

BASE_DIR      = Path("/opt/branchenradar")
DATA_DIR      = BASE_DIR / "data"
SOURCES_FILE  = DATA_DIR / "sources.json"
SIGNALS_FILE  = DATA_DIR / "signals.json"
SIGNALS_LOCK  = DATA_DIR / "signals.json.lock"
HISTORY_FILE  = DATA_DIR / "lena_history.json"
STATUS_FILE   = DATA_DIR / "lena_status.json"
AGENT_CONFIGS = DATA_DIR / "agent_configs.json"

HISTORY_MAX = 50
BATCH_SIZE  = 10   # Quellen pro API-Aufruf

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [lena] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("lena_runner")


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


def _get_search_sources(sources):
    """Quellen mit Such-/Login-Methode zurückgeben."""
    return [s for s in sources if "search" in s.get("ingestion_methods", [])]


def _source_summary(src):
    """Kompakte Zusammenfassung einer Quelle für den Prompt."""
    hint = next(
        (h.get("text", "") for h in src.get("agent_hints", []) if h.get("agent") == "search"),
        src.get("notes", "")
    )
    eps = [e for e in src.get("endpoints", []) if e.get("agent") == "search"]
    eps_str = " | ".join(e.get("url", "") + (f" [{e.get('label','')}]" if e.get("label") else "") for e in eps)

    lines = [
        f"Kürzel: {src.get('kuerzel', '')} | Name: {src.get('name', '')}",
        f"URL: {src.get('url', '')}",
        f"Kategorie: {src.get('primary_category', '')} | Zugang: {src.get('zugang', '')}",
        f"Aktualisierung: {src.get('expected_update', '')}",
    ]
    if eps_str:
        lines.append(f"Endpoints: {eps_str}")
    if hint:
        lines.append(f"Hinweis: {hint}")
    return "\n".join(lines)


def _parse_signals(text, default_source="search"):
    """JSON-Array aus der Modell-Antwort extrahieren und als Signale parsen."""
    import re
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return []
    try:
        raw = json.loads(match.group(0))
    except Exception:
        return []

    signals = []
    today = date.today().isoformat()
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or item.get("titel") or "").strip()
        if not title:
            continue
        signals.append({
            "id":          str(uuid.uuid4()),
            "title":       title,
            "summary":     (item.get("summary") or item.get("zusammenfassung") or "").strip(),
            "category":    (item.get("category") or item.get("kategorie") or "Sonstiges").strip(),
            "tags":        item.get("tags", []),
            "date":        item.get("date") or today,
            "source":      item.get("source") or default_source,
            "agent":       "search",
            "status":      "neu",
            "created_at":  today,
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
    lena_cfg = raw_cfg.get("agents", {}).get("search", {})

    persona  = lena_cfg.get("persona", "Du bist Lena, Such-/Login-Quellen-Analyse-Agent des Branchenradars für das Gesundheitswesen.")
    method   = lena_cfg.get("method", "")
    hint     = lena_cfg.get("hint", "")
    model    = lena_cfg.get("model", "claude-opus-4-8")

    sources       = _load_json(SOURCES_FILE, [])
    search_srcs   = _get_search_sources(sources)

    log.info("Starte Lena-Lauf: %d Such-Quellen gefunden", len(search_srcs))

    if not search_srcs:
        log.info("Keine Such-Quellen — Lauf wird übersprungen")
        entry = {
            "run_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "model": model, "sources_checked": 0, "batches": 0,
            "signals_new": 0, "input_tokens": 0, "output_tokens": 0,
            "duration_s": 0, "cost_usd": 0, "note": "Keine Such-Quellen verfügbar",
        }
        if not dry_run:
            history = _load_json(HISTORY_FILE, [])
            history.insert(0, entry)
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history[:HISTORY_MAX], f, ensure_ascii=False, indent=2)
            with open(STATUS_FILE, "w", encoding="utf-8") as f:
                json.dump(entry, f, ensure_ascii=False, indent=2)
        return entry

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

    batches = [search_srcs[i:i+BATCH_SIZE] for i in range(0, len(search_srcs), BATCH_SIZE)]

    for batch_idx, batch in enumerate(batches):
        sources_block = "\n\n".join(_source_summary(s) for s in batch)
        source_kuerzels = [s.get("kuerzel", "") for s in batch]

        user_message = (
            f"## Such-/Login-Quellen ({len(batch)} Quellen)\n\n"
            f"{sources_block}\n\n"
            f"Analysiere diese Such- und Login-geschützten Quellen basierend auf den verfügbaren Metadaten, "
            f"Hinweisen und bekannten Inhalten. Leite daraus relevante Signale für den Branchenradar "
            f"Gesundheitswesen ab — Regulierung, Marktentwicklungen, Versorgungs- und Vergütungsthemen.\n\n"
            f"Antworte NUR mit einem JSON-Array:\n"
            f"[\n"
            f'  {{"title": "Kurzer Titel (max 80 Zeichen)", '
            f'"summary": "Zusammenfassung (2-3 Sätze)", '
            f'"category": "Regulierung | Markt | Technologie | Personal | Sonstiges", '
            f'"tags": ["tag1", "tag2"], '
            f'"source": "KÜRZEL", '
            f'"date": "YYYY-MM-DD"}}\n'
            f"]\n\n"
            f"Maximal 3 Signale pro Quelle, insgesamt maximal 10. "
            f"Nur wenn wirklich relevante Informationen erkennbar. "
            f"Wenn keine signifikanten Signale: leeres Array []."
        )

        try:
            msg = client.messages.create(
                model=model,
                max_tokens=3000,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )
            in_tokens_total  += msg.usage.input_tokens  if msg.usage else 0
            out_tokens_total += msg.usage.output_tokens if msg.usage else 0

            response_text = "".join(b.text for b in msg.content if b.type == "text")
            new_sigs = _parse_signals(response_text, "search")
            all_new_signals.extend(new_sigs)
            log.info("Batch %d/%d: %d Signale extrahiert", batch_idx + 1, len(batches), len(new_sigs))

        except Exception as e:
            errors.append(f"Batch {batch_idx + 1}: {str(e)[:80]}")
            log.error("API-Fehler Batch %d: %s", batch_idx + 1, e)

    duration_s = round(time.time() - t_start, 1)
    cost_usd   = round(in_tokens_total / 1_000_000 * 5.0 + out_tokens_total / 1_000_000 * 25.0, 4)

    # Signale in signals.json schreiben
    if all_new_signals and not dry_run:
        existing = _load_json(SIGNALS_FILE, [])
        existing.extend(all_new_signals)
        _save_json_locked(SIGNALS_FILE, SIGNALS_LOCK, existing)
        log.info("signals.json aktualisiert: +%d Signale", len(all_new_signals))
    elif dry_run:
        log.info("DRY RUN: %d Signale würden geschrieben", len(all_new_signals))

    entry = {
        "run_at":          datetime.now().strftime("%Y-%m-%d %H:%M"),
        "model":           model,
        "sources_checked": len(search_srcs),
        "batches":         len(batches),
        "signals_new":     len(all_new_signals),
        "input_tokens":    in_tokens_total,
        "output_tokens":   out_tokens_total,
        "duration_s":      duration_s,
        "cost_usd":        cost_usd,
        "errors":          errors,
        "note":            f"{'DRY RUN — ' if dry_run else ''}{len(search_srcs)} Quellen analysiert, {len(all_new_signals)} neue Signale",
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
    parser = argparse.ArgumentParser(description="Lena Such-Quellen-Analyse-Agent")
    parser.add_argument("--dry-run", action="store_true", help="Keine Änderungen speichern")
    args = parser.parse_args()
    try:
        result = run(dry_run=args.dry_run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        log.error("Lauf fehlgeschlagen: %s", e)
        sys.exit(1)
