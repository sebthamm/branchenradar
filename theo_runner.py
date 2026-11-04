#!/usr/bin/env python3
"""
theo_runner.py — Gruppierungs-Agent für den Branchenradar.

Liest neue RAW-Signale, gruppiert inhaltliche Duplikate via Claude-API
und schreibt die Gruppen in signal_matches.json.
"""

import os
import json
import re
import uuid
import logging
from datetime import datetime
from pathlib import Path

BASE_DIR          = Path("/opt/branchenradar")
DATA_DIR          = BASE_DIR / "data"
SIGNALS_FILE      = DATA_DIR / "signals.json"
SIGNAL_MATCHES    = DATA_DIR / "signal_matches.json"
THEO_STATUS_FILE  = DATA_DIR / "theo_status.json"
THEO_HISTORY_FILE = DATA_DIR / "theo_history.json"
AGENT_CONFIGS     = DATA_DIR / "agent_configs.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [theo] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("theo_runner")

_AGENT_MODEL_PRICES = {
    "claude-haiku-4-5-20251001": (1.0,  5.0),
    "claude-sonnet-4-6":         (3.0, 15.0),
    "claude-opus-4-8":           (5.0, 25.0),
}

def _load(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}

def _save(path, data):
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def _agent_cost(model, in_tok, out_tok):
    p_in, p_out = _AGENT_MODEL_PRICES.get(model, (3.0, 15.0))
    return round((in_tok * p_in + out_tok * p_out) / 1_000_000, 4)

def _append_history(entry):
    hist = _load(THEO_HISTORY_FILE, [])
    if not isinstance(hist, list):
        hist = []
    hist.insert(0, entry)
    _save(THEO_HISTORY_FILE, hist[:10])

def run(theo_cfg: dict, api_key: str) -> dict:
    """
    Führt einen Theo-Lauf durch.
    theo_cfg: Inhalt von agent_configs["agents"]["group"]
    Gibt ein status-Dict zurück (wird auch in theo_status.json und theo_history.json geschrieben).
    """
    model = theo_cfg.get("model", "claude-haiku-4-5-20251001")
    date_from = theo_cfg.get("date_from", "")
    date_to   = theo_cfg.get("date_to", "")

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    signals = _load(SIGNALS_FILE, [])
    if date_from:
        signals = [s for s in signals if s.get("date", "") >= date_from]
    if date_to:
        signals = [s for s in signals if s.get("date", "") <= date_to]

    log.info("Starte Theo-Lauf: %d Signale, Modell %s", len(signals), model)

    sig_list = "\n".join(
        f'- ID: {s["id"]} | Titel: {s.get("title","")} | Kategorie: {s.get("category","")} | Datum: {s.get("date","")}'
        for s in signals
    )
    auto_fmt = (
        "## Ausgabeformat (automatisierter Lauf)\n\n"
        "Antworte NUR mit einem JSON-Array. Jede Gruppe ist ein Array von Signal-IDs.\n"
        "Führe nur Gruppen mit ≥2 Signalen auf. Einzelne Signale weglassen.\n"
        "Beispiel: [[\"id1\",\"id2\"],[\"id3\",\"id4\",\"id5\"]]"
    )
    prompt = (
        f'{theo_cfg.get("persona","")}\n\n'
        f'{theo_cfg.get("method","")}\n\n'
        f'{auto_fmt}\n\n'
        f'## Signale\n{sig_list}'
    )

    msg = client.messages.create(
        model=model, max_tokens=4096,
        messages=[{"role": "user", "content": prompt}]
    )
    in_tok  = msg.usage.input_tokens  if msg.usage else 0
    out_tok = msg.usage.output_tokens if msg.usage else 0
    raw_text = msg.content[0].text if msg.content else "[]"

    match = re.search(r"\[.*\]", raw_text, re.DOTALL)
    groups = json.loads(match.group(0)) if match else []

    existing = _load(SIGNAL_MATCHES, [])
    if not isinstance(existing, list):
        existing = []
    existing_ids = {
        tuple(sorted([m.get("sig1", "")] + [p.get("id", "") for p in m.get("pairs", [])]))
        for m in existing
    }
    added = 0
    for g in groups:
        ids = g if isinstance(g, list) else g.get("ids", [])
        if len(ids) < 2:
            continue
        key = tuple(sorted(ids))
        if key in existing_ids:
            continue
        existing.append({
            "id":    str(uuid.uuid4()),
            "sig1":  ids[0],
            "pairs": [{"id": i, "type": "ähnlich"} for i in ids[1:]],
        })
        existing_ids.add(key)
        added += 1
    _save(SIGNAL_MATCHES, existing)

    status = {
        "run_at":            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "model":             model,
        "groups_added":      added,
        "signals_processed": len(signals),
        "input_tokens":      in_tok,
        "output_tokens":     out_tok,
        "cost_usd":          _agent_cost(model, in_tok, out_tok),
    }
    _save(THEO_STATUS_FILE, status)
    _append_history(status)
    log.info("Theo abgeschlossen: %d Gruppen hinzugefügt, Kosten %.4f USD", added, status["cost_usd"])
    return status


if __name__ == "__main__":
    _api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not _api_key:
        raise SystemExit("ANTHROPIC_API_KEY nicht gesetzt")
    _raw_cfg = _load(AGENT_CONFIGS, {})
    _cfg = _raw_cfg.get("agents", {}).get("group", {})
    result = run(_cfg, _api_key)
    print(json.dumps(result, ensure_ascii=False, indent=2))
