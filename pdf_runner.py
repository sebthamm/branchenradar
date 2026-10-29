#!/usr/bin/env python3
"""
pdf_runner.py — Mats: PDF-Analyse-Agent für den Branchenradar.

Läuft als Standalone-Script (wird von APScheduler per _mats_scheduled_job aufgerufen)
oder direkt: python3 pdf_runner.py [--dry-run]

Ablauf:
1. Quellen mit Ingestion-Methode "pdf" laden
2. PDF-Endpoints laden und Dateien per HTTP abrufen
3. Prompt aufbauen (Persona + Methode + Hint + Quellen-Context)
4. Anthropic API mit PDF-Anhängen aufrufen (claude-opus-4-8)
5. Signale aus der Antwort parsen
6. Neue Signale in signals.json schreiben (mit Lock)
7. Lauf-Eintrag in mats_history.json schreiben
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

BASE_DIR     = Path("/opt/branchenradar")
DATA_DIR     = BASE_DIR / "data"
SOURCES_FILE = DATA_DIR / "sources.json"
SIGNALS_FILE = DATA_DIR / "signals.json"
SIGNALS_LOCK = DATA_DIR / "signals.json.lock"
HISTORY_FILE = DATA_DIR / "mats_history.json"
STATUS_FILE  = DATA_DIR / "mats_status.json"
AGENT_CONFIGS = DATA_DIR / "agent_configs.json"

HISTORY_MAX = 50

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [mats] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mats_runner")


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


def _get_pdf_sources(sources):
    """Quellen mit PDF-Endpoints zurückgeben."""
    result = []
    for s in sources:
        methods = s.get("ingestion_methods", [])
        if "pdf" not in methods:
            continue
        eps = [e for e in s.get("endpoints", []) if e.get("agent") == "pdf" and e.get("url")]
        if eps:
            result.append((s, eps))
    return result


def _fetch_pdf(url, timeout=15):
    """PDF-Datei herunterladen und als bytes zurückgeben."""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; BranchenradarMats/1.0)"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            data = resp.read()
            if "pdf" in content_type.lower() or url.lower().endswith(".pdf") or data[:4] == b"%PDF":
                return data
            return None
    except Exception as e:
        log.warning("PDF-Abruf fehlgeschlagen %s: %s", url, e)
        return None


def _parse_signals(text, source_kuerzel):
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
            "source":      source_kuerzel,
            "agent":       "pdf",
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
    mats_cfg = raw_cfg.get("agents", {}).get("pdf", {})

    persona  = mats_cfg.get("persona", "Du bist Mats, PDF-Analyse-Agent des Branchenradars für das Gesundheitswesen.")
    method   = mats_cfg.get("method", "")
    hint     = mats_cfg.get("hint", "")
    model    = mats_cfg.get("model", "claude-opus-4-8")

    sources  = _load_json(SOURCES_FILE, [])
    pdf_srcs = _get_pdf_sources(sources)

    log.info("Starte Mats-Lauf: %d PDF-Quellen gefunden", len(pdf_srcs))

    if not pdf_srcs:
        log.info("Keine PDF-Quellen mit Endpoints — Lauf wird übersprungen")
        entry = {
            "run_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "model": model, "sources_checked": 0, "pdfs_fetched": 0,
            "signals_new": 0, "input_tokens": 0, "output_tokens": 0,
            "duration_s": 0, "cost_usd": 0, "note": "Keine PDF-Quellen verfügbar",
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

    all_new_signals = []
    in_tokens_total  = 0
    out_tokens_total = 0
    pdfs_fetched     = 0
    errors           = []

    for src, eps in pdf_srcs:
        kuerzel = src.get("kuerzel", "")
        name    = src.get("name", "")
        hint_text = next(
            (h.get("text", "") for h in src.get("agent_hints", []) if h.get("agent") == "pdf"),
            src.get("notes", "")
        )

        for ep in eps[:3]:  # Maximal 3 Endpoints pro Quelle
            url = ep.get("url", "")
            label = ep.get("label", "")

            pdf_bytes = _fetch_pdf(url)
            if not pdf_bytes:
                errors.append(f"{kuerzel}: PDF nicht abrufbar ({url[:60]})")
                continue
            pdfs_fetched += 1

            system_prompt = "\n\n".join(filter(None, [
                persona,
                f"## Methode\n{method}" if method else "",
                f"## Hinweise\n{hint}" if hint else "",
            ]))

            user_message = (
                f"## Quelle\nKürzel: {kuerzel} | Name: {name}"
                + (f" | Label: {label}" if label else "")
                + (f"\nHinweis: {hint_text}" if hint_text else "")
                + f"\n\nAnalysiere das beigefügte PDF und extrahiere relevante Signale für den Branchenradar Gesundheitswesen.\n\n"
                f"Antworte NUR mit einem JSON-Array:\n"
                f"[\n"
                f'  {{"title": "Kurzer Titel (max 80 Zeichen)", '
                f'"summary": "Zusammenfassung (2-3 Sätze)", '
                f'"category": "Regulierung | Markt | Technologie | Personal | Sonstiges", '
                f'"tags": ["tag1", "tag2"], '
                f'"date": "YYYY-MM-DD"}}\n'
                f"]\n\n"
                f"Maximal 5 Signale. Nur wenn wirklich relevante Inhalte vorhanden. "
                f"Wenn keine signifikanten Signale: leeres Array []."
            )

            import base64
            pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")

            try:
                msg = client.messages.create(
                    model=model,
                    max_tokens=2048,
                    system=system_prompt,
                    messages=[{
                        "role": "user",
                        "content": [
                            {
                                "type": "document",
                                "source": {
                                    "type": "base64",
                                    "media_type": "application/pdf",
                                    "data": pdf_b64,
                                },
                            },
                            {"type": "text", "text": user_message},
                        ],
                    }],
                )
                in_tokens_total  += msg.usage.input_tokens  if msg.usage else 0
                out_tokens_total += msg.usage.output_tokens if msg.usage else 0

                response_text = "".join(b.text for b in msg.content if b.type == "text")
                new_sigs = _parse_signals(response_text, kuerzel)
                all_new_signals.extend(new_sigs)
                log.info("%s: %d Signale aus PDF extrahiert", kuerzel, len(new_sigs))

            except Exception as e:
                errors.append(f"{kuerzel}: API-Fehler — {str(e)[:80]}")
                log.error("API-Fehler für %s: %s", kuerzel, e)

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
        "sources_checked": len(pdf_srcs),
        "pdfs_fetched":    pdfs_fetched,
        "signals_new":     len(all_new_signals),
        "input_tokens":    in_tokens_total,
        "output_tokens":   out_tokens_total,
        "duration_s":      duration_s,
        "cost_usd":        cost_usd,
        "errors":          errors,
        "note":            f"{'DRY RUN — ' if dry_run else ''}{pdfs_fetched} PDFs analysiert, {len(all_new_signals)} neue Signale",
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
    parser = argparse.ArgumentParser(description="Mats PDF-Analyse-Agent")
    parser.add_argument("--dry-run", action="store_true", help="Keine Änderungen speichern")
    args = parser.parse_args()
    try:
        result = run(dry_run=args.dry_run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        log.error("Lauf fehlgeschlagen: %s", e)
        sys.exit(1)
