"""
Maja Runner — vollautomatische Quellen-Pflege via Claude API.
Verarbeitet sources.json in Batches, schreibt Endpoints direkt zurück.
"""
import json
import os
import re
from datetime import datetime

import anthropic

DATA_DIR      = os.path.join(os.path.dirname(__file__), "data")
SOURCES_FILE  = os.path.join(DATA_DIR, "sources.json")
STATUS_FILE   = os.path.join(DATA_DIR, "maja_status.json")

BATCH_SIZE = 15

# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_sources():
    with open(SOURCES_FILE, encoding="utf-8") as f:
        return json.load(f)

def _save_sources(sources):
    with open(SOURCES_FILE, "w", encoding="utf-8") as f:
        json.dump(sources, f, ensure_ascii=False, indent=2)

def _source_to_tsv_row(s):
    eps = s.get("endpoints", [])
    ep_str = " | ".join(
        f"{e.get('agent','')}:{e.get('url','')} ({e.get('label','')})"
        + (f" [{e['selector']}]" if e.get("selector") else "")
        for e in eps
    )
    hints = s.get("agent_hints", [])
    def hint(agent):
        h = next((h["text"] for h in hints if h.get("agent") == agent), "")
        return h

    return "\t".join([
        s.get("kuerzel", ""),
        s.get("name", ""),
        s.get("url", ""),
        s.get("region", "Bundesweit"),
        ep_str,
        s.get("primary_category", ""),
        ", ".join(s.get("relevant_roles", [])),
        ", ".join(s.get("ingestion_methods", [])),
        s.get("priority", ""),
        s.get("zugang", ""),
        s.get("expected_update", ""),
        s.get("status", ""),
        s.get("notes", ""),
        hint("scrape"),
        hint("feed"),
        hint("pdf"),
        hint("search"),
        s.get("created_at", ""),
        "",  # letzte Änderung — wird von Maja befüllt
        "",  # Kommentar
    ])

TSV_HEADER = (
    "Kürzel\tName\tURL\tRegion\tEndpoints\tPrimärkategorie\tRelevante Rollen\tAgenten\t"
    "Priorität\tZugang\tAktualisierung\tStatus\tHinweise\t"
    "Agenten-Hinweis (scrape)\tAgenten-Hinweis (feed)\tAgenten-Hinweis (pdf)\tAgenten-Hinweis (search)\t"
    "Datum Hinzugefügt\tDatum Letzte Änderung\tKommentar"
)

def _parse_endpoints(eps_raw):
    endpoints = []
    if not eps_raw:
        return endpoints
    for ep_str in eps_raw.split(" | "):
        ep_str = ep_str.strip()
        if not ep_str:
            continue
        label = ""
        selector = ""
        if "[" in ep_str and ep_str.endswith("]"):
            ep_str, selector = ep_str[:-1].rsplit("[", 1)
            selector = selector.strip()
            ep_str = ep_str.strip()
        if "(" in ep_str and ep_str.endswith(")"):
            ep_str, label = ep_str[:-1].rsplit("(", 1)
            label = label.strip()
            ep_str = ep_str.strip()
        if ":" in ep_str:
            agent_key, url = ep_str.split(":", 1)
        else:
            agent_key, url = "", ep_str
        if url.strip():
            ep = {"url": url.strip(), "agent": agent_key.strip(), "label": label}
            if selector:
                ep["selector"] = selector
            endpoints.append(ep)
    return endpoints

def _parse_tsv_response(tsv_text):
    """Parse Maja's TSV output into a list of partial source dicts keyed by kuerzel."""
    lines = [l for l in tsv_text.strip().splitlines() if l.strip()]
    if not lines:
        return {}
    # Skip header
    start = 1 if "\t" in lines[0] and ("kürzel" in lines[0].lower() or "name" in lines[0].lower()) else 0
    result = {}
    for line in lines[start:]:
        parts = line.split("\t")
        def c(i): return parts[i].strip() if i < len(parts) else ""
        kuerzel = c(0).upper()
        name    = c(1)
        if not name:
            continue
        endpoints = _parse_endpoints(c(4))
        methods_raw = c(7)
        methods = [m.strip() for m in methods_raw.replace(",", " ").split() if m.strip() in ("scrape", "feed", "pdf", "search")]
        roles = [r.strip() for r in c(6).split(",") if r.strip()]
        agent_hints = []
        for i, agent in enumerate(["scrape", "feed", "pdf", "search"], start=13):
            text = c(i)
            if text:
                agent_hints.append({"agent": agent, "text": text})
        result[kuerzel] = {
            "url":               c(2) or None,
            "endpoints":         endpoints,
            "ingestion_methods": methods or None,
            "relevant_roles":    roles or None,
            "agent_hints":       agent_hints or None,
            "notes":             c(12) or None,
            "zugang":            c(9) or None,
            "expected_update":   c(10) or None,
        }
    return result

def _build_prompt(persona, method, hint, output_format, sources_batch):
    tsv_rows = "\n".join([_source_to_tsv_row(s) for s in sources_batch])
    return f"""{persona}

## Methode
{method}

## Globale Hinweise
{hint}

{output_format}

---

## Zu pflegende Quellen

{TSV_HEADER}
{tsv_rows}"""

# ── Main runner ───────────────────────────────────────────────────────────────

def run(agent_cfg, api_key, model="claude-haiku-4-5-20251001"):
    sources  = _load_sources()
    run_at   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    persona  = agent_cfg.get("persona", "")
    method   = agent_cfg.get("method", "")
    hint     = agent_cfg.get("hint", "")
    out_fmt  = agent_cfg.get("output_format", "")

    client = anthropic.Anthropic(api_key=api_key)

    batches = [sources[i:i+BATCH_SIZE] for i in range(0, len(sources), BATCH_SIZE)]
    updated = 0
    errors  = []

    for batch_idx, batch in enumerate(batches):
        prompt = _build_prompt(persona, method, hint, out_fmt, batch)
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text if msg.content else ""
            # Extract TSV block (between ``` or raw)
            tsv_match = re.search(r"```(?:tsv)?\n(.*?)```", raw, re.DOTALL)
            tsv_text  = tsv_match.group(1) if tsv_match else raw
            parsed    = _parse_tsv_response(tsv_text)

            for src in sources:
                key = src.get("kuerzel", "").upper()
                if key not in parsed:
                    continue
                patch = parsed[key]
                # Only update endpoints if Maja returned some
                if patch.get("endpoints"):
                    src["endpoints"] = patch["endpoints"]
                    updated += 1
                if patch.get("ingestion_methods"):
                    src["ingestion_methods"] = patch["ingestion_methods"]
                if patch.get("relevant_roles"):
                    src["relevant_roles"] = patch["relevant_roles"]
                if patch.get("agent_hints"):
                    existing = {h["agent"]: h for h in src.get("agent_hints", [])}
                    for h in patch["agent_hints"]:
                        existing[h["agent"]] = h
                    src["agent_hints"] = list(existing.values())
                for field in ("notes", "zugang", "expected_update"):
                    if patch.get(field):
                        src[field] = patch[field]
                if patch.get("url"):
                    src["url"] = patch["url"]

        except Exception as e:
            errors.append(f"Batch {batch_idx + 1}: {str(e)[:120]}")

    _save_sources(sources)

    status = {
        "run_at":        run_at,
        "sources_total": len(sources),
        "batches":       len(batches),
        "endpoints_added": updated,
        "errors":        errors,
    }
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)

    return status
