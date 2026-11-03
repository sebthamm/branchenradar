#!/usr/bin/env python3
"""
nils_runner.py — Automatischer Quellen-Recherche-Agent für den Branchenradar.

Läuft als Standalone-Script (wird von APScheduler per _nils_scheduled_job aufgerufen)
oder direkt: python3 nils_runner.py [--dry-run] [--focus "Fokus-Beschreibung"]

Ablauf:
1. Aktuelle Quellenliste als TSV laden
2. Prompt aufbauen (Persona + Methode + Hint + Ausgabeformat + Quellenliste)
3. Anthropic API aufrufen (claude-opus-4-8, extended_thinking)
4. Antwort parsen (TSV extrahieren)
5. Neue Quellen identifizieren (Kürzel-Vergleich)
6. Neue Quellen in sources.json schreiben (mit Lock)
7. Lauf-Eintrag in nils_history.json schreiben
"""

import os
import sys
import json
import re
import time
import uuid
import fcntl
import logging
from datetime import datetime, date
from pathlib import Path

# ── Pfade ──────────────────────────────────────────────────────────────────────
BASE_DIR         = Path("/opt/branchenradar")
DATA_DIR         = BASE_DIR / "data"
SOURCES_FILE     = DATA_DIR / "sources.json"
SOURCES_LOCK     = DATA_DIR / "sources.json.lock"
NILS_HISTORY     = DATA_DIR / "nils_history.json"
AGENT_CONFIGS    = DATA_DIR / "agent_configs.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [nils] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("nils_runner")

# ── Hilfsfunktionen ────────────────────────────────────────────────────────────

def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _save_json_locked(path, lock_path, data):
    """JSON atomar schreiben mit fcntl-Lock."""
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            tmp = str(path) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)

def sources_to_tsv(sources: list) -> str:
    """Quellenliste als TSV-String (identisch zum Export in app.py)."""
    header = (
        "Kürzel\tName\tURL\tRegion\tEndpoints\tPrimärkategorie\tRelevante Rollen\t"
        "Agenten\tPriorität\tZugang\tAktualisierung\tStatus\tHinweise\t"
        "Agenten-Hinweis (scrape)\tAgenten-Hinweis (feed)\t"
        "Agenten-Hinweis (pdf)\tAgenten-Hinweis (search)\t"
        "Datum Hinzugefügt\tDatum Letzte Änderung\tKommentar"
    )
    rows = [header]
    for s in sorted(sources, key=lambda x: x.get("kuerzel", "")):
        ah = {h.get("agent", ""): h.get("text", "") for h in s.get("agent_hints", [])}
        eps = s.get("endpoints", [])
        eps_str = " | ".join(
            f"{e.get('agent','')}:{e.get('url','')} ({e.get('label','')})"
            for e in eps
        ) if eps else ""
        rows.append("\t".join([
            s.get("kuerzel", ""),
            s.get("name", ""),
            s.get("url", ""),
            s.get("region", "Bundesweit"),
            eps_str,
            s.get("primary_category", ""),
            ", ".join(s.get("relevant_roles", [])),
            ", ".join(s.get("ingestion_methods", [])),
            s.get("priority", ""),
            s.get("zugang", ""),
            s.get("expected_update", ""),
            s.get("status", ""),
            s.get("notes", ""),
            ah.get("scrape", ""),
            ah.get("feed", ""),
            ah.get("pdf", ""),
            ah.get("search", ""),
            s.get("created_at", ""),
            s.get("updated_at", ""),
            s.get("comment", ""),
        ]))
    return "\n".join(rows)

def parse_tsv_response(text: str) -> list[dict]:
    """TSV-Block aus der Modell-Antwort extrahieren und parsen."""
    # Suche nach dem TSV-Block (beginnt mit "Kürzel" oder "Kurzel")
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(r"K.?rzel\t", line):
            start = i
            break
    if start is None:
        # Fallback: suche nach erster Zeile mit mindestens 5 Tabs
        for i, line in enumerate(lines):
            if line.count("\t") >= 5:
                start = i
                break
    if start is None:
        return []

    tsv_lines = []
    for line in lines[start:]:
        if not line.strip():
            # Leere Zeile = Ende des TSV-Blocks
            if tsv_lines:
                break
            continue
        # Zeile mit weniger als 3 Tabs ignorieren (z.B. Kommentare zwischen Blöcken)
        if line.count("\t") < 3:
            break
        tsv_lines.append(line)

    if len(tsv_lines) < 2:
        return []

    header = [h.strip().lower() for h in tsv_lines[0].split("\t")]

    def col(name_patterns):
        for pat in name_patterns:
            for i, h in enumerate(header):
                if pat in h:
                    return i
        return None

    idx = {
        "kuerzel":          col(["kürzel", "kurzel"]),
        "name":             col(["name"]),
        "url":              col(["url"]),
        "region":           col(["region"]),
        "endpoints":        col(["endpoint"]),
        "primary_category": col(["primär", "kategorie"]),
        "relevant_roles":   col(["rollen"]),
        "ingestion_methods":col(["agenten"]),
        "priority":         col(["priorität", "prioritat"]),
        "zugang":           col(["zugang"]),
        "expected_update":  col(["aktualis"]),
        "status":           col(["status"]),
        "notes":            col(["hinweise"]),
        "hint_scrape":      col(["scrape"]),
        "hint_feed":        col(["feed"]),
        "hint_pdf":         col(["pdf"]),
        "hint_search":      col(["search"]),
        "created_at":       col(["hinzugefügt", "hinzugefugt"]),
        "updated_at":       col(["letzte"]),
        "comment":          col(["kommentar"]),
    }

    def cell(parts, key):
        i = idx.get(key)
        if i is None or i >= len(parts):
            return ""
        return parts[i].strip()

    results = []
    for line in tsv_lines[1:]:
        parts = line.split("\t")
        name = cell(parts, "name")
        if not name:
            continue

        agent_hints = []
        for ak in ("hint_scrape", "hint_feed", "hint_pdf", "hint_search"):
            t = cell(parts, ak)
            if t:
                agent_hints.append({"agent": ak.replace("hint_", ""), "text": t})

        methods_raw = cell(parts, "ingestion_methods")
        methods = [m.strip() for m in re.split(r"[,\s]+", methods_raw)
                   if m.strip() in ("scrape", "feed", "pdf", "search")]

        roles = [r.strip() for r in cell(parts, "relevant_roles").split(",") if r.strip()]

        eps_raw = cell(parts, "endpoints")
        endpoints = []
        if eps_raw:
            for ep_str in eps_raw.split(" | "):
                ep_str = ep_str.strip()
                if not ep_str:
                    continue
                label = ""
                if "(" in ep_str and ep_str.endswith(")"):
                    ep_str, label = ep_str[:-1].rsplit("(", 1)
                    label = label.strip()
                    ep_str = ep_str.strip()
                if ":" in ep_str:
                    agent_key, url = ep_str.split(":", 1)
                else:
                    agent_key, url = "", ep_str
                if url.strip():
                    endpoints.append({"url": url.strip(), "agent": agent_key.strip(), "label": label})

        results.append({
            "id":               str(uuid.uuid4()),
            "kuerzel":          cell(parts, "kuerzel"),
            "name":             name,
            "url":              cell(parts, "url"),
            "region":           cell(parts, "region") or "Bundesweit",
            "endpoints":        endpoints,
            "primary_category": cell(parts, "primary_category"),
            "relevant_roles":   roles,
            "ingestion_methods":methods,
            "priority":         cell(parts, "priority") or "mittel",
            "zugang":           cell(parts, "zugang"),
            "expected_update":  cell(parts, "expected_update"),
            "status":           cell(parts, "status"),
            "notes":            cell(parts, "notes"),
            "agent_hints":      agent_hints,
            "created_at":       cell(parts, "created_at"),
            "updated_at":       cell(parts, "updated_at"),
            "comment":          cell(parts, "comment"),
        })
    return results

# ── Hauptfunktion ──────────────────────────────────────────────────────────────


NILS_INSIGHTS_FILE = os.path.join(os.path.dirname(SOURCES_FILE), "nils_insights.json")

def _load_nils_insights():
    if not os.path.exists(NILS_INSIGHTS_FILE):
        return []
    with open(NILS_INSIGHTS_FILE, encoding="utf-8") as f:
        return json.load(f)

def _save_nils_insights(insights):
    with open(NILS_INSIGHTS_FILE, "w", encoding="utf-8") as f:
        json.dump(insights, f, ensure_ascii=False, indent=2)

def _generate_nils_insights(client, model, new_sources, all_sources, run_at):
    """Zweiter Claude-Call: systemische Erkenntnisse aus dem Nils-Lauf ableiten."""
    if not new_sources:
        return
    cats = {}
    roles = {}
    missing_region = 0
    missing_agents = 0
    for s in new_sources:
        c = s.get("primaerkategorie") or s.get("category") or "Unbekannt"
        cats[c] = cats.get(c, 0) + 1
        for r in (s.get("relevante_rollen") or "").split(","):
            r = r.strip()
            if r:
                roles[r] = roles.get(r, 0) + 1
        if not s.get("region"):
            missing_region += 1
        if not s.get("agents") and not s.get("endpoints"):
            missing_agents += 1

    summary = f"""Du bist Nils, Quellen-Recherche-Agent für den Branchenradar Gesundheitswesen.
Du hast gerade {len(new_sources)} neue Quellen gefunden (Gesamtbestand: {len(all_sources)}).

Statistik des Laufs:
- Neue Kategorien: {json.dumps(cats, ensure_ascii=False)}
- Neue Rollen (Top): {json.dumps(dict(sorted(roles.items(), key=lambda x: -x[1])[:8]), ensure_ascii=False)}
- Neue Quellen ohne Region: {missing_region} von {len(new_sources)}
- Neue Quellen ohne Agenten-Zuweisung: {missing_agents} von {len(new_sources)}

Leite maximal 3 inhaltliche Erkenntnisse ab. Fokus ausschließlich auf:
- Inhaltliche Lücken: Welche Themenfelder, Fachgebiete oder Institutionstypen fehlen noch?
- Recherchefelder: Welche konkreten Organisationsgruppen (z.B. KV-Verbände, Fachgesellschaften) sind noch unvollständig abgedeckt?
- Qualitätsmuster: Auffälligkeiten bei den gefundenen Quellen, die auf Schwächen in der Recherche-Strategie hindeuten.

NICHT berichten über: Endpoint-Typen-Verteilung, Scrape vs. Feed-Verhältnis, fehlende Region-Attribute, Endpoint-Anzahl pro Quelle.
Diese technischen Aspekte werden separat verwaltet.

Antworte NUR mit einem JSON-Array, ohne Erklärung davor oder danach:
[
  {{
    "category": "Lücken | Recherchefeld | Qualität",
    "title": "Kurzer Titel (max 60 Zeichen)",
    "description": "Konkrete Beschreibung: was fehlt, welche Organisationen, warum relevant (2-3 Sätze)"
  }}
]

Maximal 3 Vorschläge. Nur wenn du wirklich etwas Konkretes und Neues siehst."""

    try:
        import anthropic as _ant
        msg = client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": summary}],
        )
        raw = msg.content[0].text if msg.content else "[]"
        import re as _re
        match = _re.search(r"\[.*\]", raw, _re.DOTALL)
        proposals = json.loads(match.group(0)) if match else []
    except Exception:
        proposals = []

    if not proposals:
        return

    existing = _load_nils_insights()

    def _title_words(t):
        return set(w.lower() for w in re.split(r'\W+', t) if len(w) > 3)

    def _is_duplicate(title, existing_list, days=60):
        """True wenn ein inhaltlich ähnlicher Titel in den letzten `days` Tagen existiert."""
        from datetime import timedelta
        cutoff_dt = datetime.now() - timedelta(days=days)
        new_words = _title_words(title)
        if not new_words:
            return False
        for ex in existing_list:
            try:
                ex_dt = datetime.strptime(ex["run_at"][:16], "%Y-%m-%d %H:%M")
            except Exception:
                continue
            if ex_dt < cutoff_dt:
                continue
            ex_words = _title_words(ex.get("title", ""))
            if not ex_words:
                continue
            overlap = len(new_words & ex_words) / min(len(new_words), len(ex_words))
            if overlap >= 0.55:
                return True
        return False

    saved = 0
    for p in proposals:
        title = p.get("title", "")[:120]
        if _is_duplicate(title, existing):
            log.info(f"Insight übersprungen (Duplikat): {title}")
            continue
        existing.insert(0, {
            "id":          str(uuid.uuid4()),
            "run_at":      run_at,
            "category":    p.get("category", "Sonstiges"),
            "title":       title,
            "description": p.get("description", ""),
            "status":      "neu",
        })
        saved += 1
    _save_nils_insights(existing[:50])
    log.info(f"Nils insights: {len(proposals)} generiert, {saved} neu gespeichert ({len(proposals)-saved} Duplikate übersprungen)")

def run(focus: str = "", dry_run: bool = False) -> dict:
    """
    Führt einen Nils-Lauf durch.
    Gibt ein result-Dict zurück, das direkt in nils_history.json geschrieben wird.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY nicht gesetzt")

    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic-Paket nicht installiert (pip install anthropic)")

    # Konfig laden
    raw_cfg = _load_json(AGENT_CONFIGS, {})
    nils_cfg = raw_cfg.get("agents", {}).get("source_research", {})

    persona       = nils_cfg.get("persona", "")
    method        = nils_cfg.get("method", "")
    hint          = nils_cfg.get("hint", "")
    output_format = nils_cfg.get("output_format", "")
    model         = nils_cfg.get("nils_model", "claude-opus-4-8")

    # Quellen laden
    sources = _load_json(SOURCES_FILE, [])
    sources_before = len(sources)
    existing_kuerzel = {s.get("kuerzel", "").upper() for s in sources if s.get("kuerzel")}

    tsv_context = sources_to_tsv(sources)

    # Ausgabeformat fuer automatische Laeufe: nur neue Quellen (spart Tokens)
    tsv_header = (
        "Kürzel\tName\tURL\tRegion\tEndpoints\tPrimärkategorie\tRelevante Rollen\t"
        "Agenten\tPriorität\tZugang\tAktualisierung\tStatus\tHinweise\t"
        "Agenten-Hinweis (scrape)\tAgenten-Hinweis (feed)\t"
        "Agenten-Hinweis (pdf)\tAgenten-Hinweis (search)\t"
        "Datum Hinzugefügt\tDatum Letzte Änderung\tKommentar"
    )
    today = date.today().isoformat()

    focus_text = focus if focus else (
        "Allgemeine Recherche: Suche nach neuen relevanten Quellen aus allen Kategorien "
        "die noch nicht in der Liste enthalten sind. Schwerpunkt auf Bundesbehörden, "
        "KV-Landesverbände, Fachgesellschaften und Selbstverwaltungsorgane."
    )

    system_prompt = "\n\n".join(filter(None, [
        persona,
        f"## Methode\n{method}" if method else "",
        f"## Hinweise\n{hint}" if hint else "",
    ]))

    kuerzel_list = chr(10).join(sorted(existing_kuerzel))
    user_message = (
        f"## Suchfokus\n{focus_text}\n\n"
        f"## Bestehende Kürzel ({sources_before} Quellen bereits registriert)\n"
        f"Diese Kürzel sind bereits vorhanden — keine Duplikate:\n"
        f"{kuerzel_list}\n\n"
        f"## Aufgabe\n"
        f"Recherchiere 5-15 neue Quellen die noch NICHT in der Liste sind und zum Fokus passen. "
        f"Gib NUR die neuen Quellen zurück (keine bestehenden). "
        f"Datum Hinzugefügt: {today}. Kommentar: 'Neu hinzugefügt'.\n\n"
        f"## Ausgabeformat\n"
        f"Liefere die neuen Quellen als TSV mit genau dieser Kopfzeile:\n\n"
        f"{tsv_header}\n\n"
        f"Regeln:\n"
        f"- 'URL': direkter Einstiegspunkt (Unterseite/Feed/Dokumentverzeichnis), nicht die Homepage\n"
        f"- 'Agenten': kommagetrennt aus scrape | feed | pdf | search\n"
        f"- 'Relevante Rollen': kommagetrennt\n"
        f"- 'Kürzel': maximal 8 Zeichen, Großbuchstaben, eindeutig\n"
        f"- 'Region': PFLICHTFELD. Wähle aus: Bundesweit | Bayern | NRW | Baden-Württemberg | "
        f"Berlin | Brandenburg | Bremen | Hamburg | Hessen | Mecklenburg-Vorpommern | "
        f"Niedersachsen | Rheinland-Pfalz | Saarland | Sachsen | Sachsen-Anhalt | "
        f"Schleswig-Holstein | Thüringen | Regional (mehrere Bundesländer)\n"
        f"Wenn du keine neuen Quellen findest, schreibe: KEINE_NEUEN_QUELLEN"
    )

    # ── Kostenlimits berechnen ────────────────────────────────────────────────
    raw_cfg2     = _load_json(AGENT_CONFIGS, {})
    nils_limits  = raw_cfg2.get("agents", {}).get("source_research", {})
    abs_limit_usd   = float(nils_limits.get("cost_limit_usd",  0) or 0)
    rel_limit_pct   = float(nils_limits.get("cost_limit_rel",  0) or 0)  # % über Durchschnitt

    # Schätzung: input tokens ~ prompt_chars / 4
    est_input_tokens  = len(system_prompt + user_message) // 4
    # Modell-Preise: opus-4-8 $5/1M in, $25/1M out
    PRICE_IN  = 5.0 / 1_000_000
    PRICE_OUT = 25.0 / 1_000_000

    # Maximale Output-Tokens aus absolutem Limit ableiten
    max_out_from_abs = 16000  # default
    if abs_limit_usd > 0:
        cost_input_only = est_input_tokens * PRICE_IN
        budget_for_output = abs_limit_usd - cost_input_only
        if budget_for_output <= 0:
            msg = (f"Kostenlimit von {abs_limit_usd:.2f} USD würde bereits durch Input-Tokens "
                   f"(~{est_input_tokens} ≈ {cost_input_only:.4f} USD) überschritten.")
            log.warning(msg)
            if not dry_run:
                _notify_limit(msg, abs_limit_usd, "absolut")
            return {
                "run_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "model": model,
                "sources_before": sources_before, "sources_after": sources_before,
                "new_sources": 0, "input_tokens": 0, "output_tokens": 0,
                "duration_s": 0, "cost_usd": 0,
                "note": f"GESTOPPT: {msg}",
            }
        max_out_from_abs = int(budget_for_output / PRICE_OUT)

    # Relatives Limit: Durchschnitt aus History
    if rel_limit_pct > 0:
        history_check = _load_json(NILS_HISTORY, [])
        past_costs = [e.get("cost_usd", 0) for e in history_check[:20] if e.get("cost_usd", 0) > 0]
        if past_costs:
            avg_cost = sum(past_costs) / len(past_costs)
            max_allowed = avg_cost * (1 + rel_limit_pct / 100)
            budget_rel  = max_allowed - est_input_tokens * PRICE_IN
            if budget_rel > 0:
                max_out_from_rel = int(budget_rel / PRICE_OUT)
                max_out_from_abs = min(max_out_from_abs, max_out_from_rel)
                log.info(f"Relatives Limit: avg={avg_cost:.4f} USD, max={max_allowed:.4f} USD → max_tokens={max_out_from_abs}")

    # max_tokens mindestens 500, höchstens 16000
    safe_max_tokens = max(500, min(16000, max_out_from_abs))
    log.info(f"Starte Nils-Lauf: {sources_before} Quellen, Modell {model}, max_tokens={safe_max_tokens}")
    t_start = time.time()

    client = anthropic.Anthropic(api_key=api_key)

    # API-Aufruf mit adaptive thinking
    response = client.messages.create(
        model=model,
        max_tokens=safe_max_tokens,
        thinking={"type": "adaptive"},
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )

    duration_s = round(time.time() - t_start, 1)

    # Token-Zählung
    input_tokens  = response.usage.input_tokens
    output_tokens = response.usage.output_tokens

    # Kosten (claude-opus-4-8: $5/1M in, $25/1M out)
    cost_usd = round(input_tokens / 1_000_000 * 5.0 + output_tokens / 1_000_000 * 25.0, 4)

    # Relatives Limit: tatsächliche Kosten prüfen (post-run check)
    if rel_limit_pct > 0 and not dry_run:
        history_check2 = _load_json(NILS_HISTORY, [])
        past_costs2 = [e.get("cost_usd", 0) for e in history_check2[:20] if e.get("cost_usd", 0) > 0]
        if past_costs2:
            avg_cost2   = sum(past_costs2) / len(past_costs2)
            max_allowed2 = avg_cost2 * (1 + rel_limit_pct / 100)
            if cost_usd > max_allowed2:
                msg2 = (f"Tatsächliche Kosten {cost_usd:.4f} USD überschreiten relatives Limit "
                        f"({rel_limit_pct:.0f}% über Ø {avg_cost2:.4f} USD = {max_allowed2:.4f} USD).")
                log.warning(msg2)
                _notify_limit(msg2, max_allowed2, "relativ")

    # Antwort-Text extrahieren
    response_text = ""
    for block in response.content:
        if block.type == "text":
            response_text += block.text

    log.info(f"API-Antwort erhalten: {input_tokens} in / {output_tokens} out / {duration_s}s")

    # TSV parsen
    parsed = parse_tsv_response(response_text)
    log.info(f"TSV geparst: {len(parsed)} Zeilen")

    # Neue Quellen identifizieren
    new_sources = []
    for src in parsed:
        k = src.get("kuerzel", "").upper()
        name = src.get("name", "")
        if not name:
            continue
        is_new = (
            k and k not in existing_kuerzel
        ) or (
            not k and not any(s.get("name", "").lower() == name.lower() for s in sources)
        )
        if is_new:
            if not src.get("created_at"):
                src["created_at"] = date.today().isoformat()
            if not src.get("id"):
                src["id"] = str(uuid.uuid4())
            new_sources.append(src)

    sources_after = sources_before + len(new_sources)
    log.info(f"Neue Quellen gefunden: {len(new_sources)}")

    # Quellen schreiben (wenn nicht dry-run)
    if new_sources and not dry_run:
        updated_sources = sources + new_sources
        _save_json_locked(SOURCES_FILE, SOURCES_LOCK, updated_sources)
        log.info(f"sources.json aktualisiert: {sources_before} → {sources_after} Quellen")
    elif dry_run:
        log.info("DRY RUN: keine Änderungen gespeichert")

    # History-Eintrag
    entry = {
        "run_at":         datetime.now().strftime("%Y-%m-%d %H:%M"),
        "model":          model,
        "sources_before": sources_before,
        "sources_after":  sources_after,
        "new_sources":    len(new_sources),
        "input_tokens":   input_tokens,
        "output_tokens":  output_tokens,
        "duration_s":     duration_s,
        "cost_usd":       cost_usd,
        "note":           f"Automatischer Lauf{' (dry-run)' if dry_run else ''}. {len(new_sources)} neue Quellen." if not focus
                          else f"Fokus: {focus[:80]}. {len(new_sources)} neue Quellen.",
    }

    if not dry_run:
        history = _load_json(NILS_HISTORY, [])
        history.insert(0, entry)
        with open(NILS_HISTORY, "w", encoding="utf-8") as f:
            json.dump(history[:50], f, ensure_ascii=False, indent=2)
        if new_sources:
            try:
                _generate_nils_insights(client, model, new_sources, sources + new_sources,
                                        entry["run_at"])
            except Exception as _ie:
                log.warning(f"Nils insights fehlgeschlagen: {_ie}")

    return entry


# ── CLI-Wrapper ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Nils Quellen-Recherche-Agent")
    parser.add_argument("--dry-run", action="store_true", help="Keine Änderungen speichern")
    parser.add_argument("--focus", default="", help="Optionaler Suchfokus")
    args = parser.parse_args()

    try:
        result = run(focus=args.focus, dry_run=args.dry_run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        log.error(f"Lauf fehlgeschlagen: {e}")
        sys.exit(1)
