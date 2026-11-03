#!/usr/bin/env python3
"""
Einmaliger Datenfix: Ungültige Endpoint-Typen in sources.json bereinigen.
Führe aus auf dem Server: python3 fix_endpoint_types.py
"""
import json, os

SOURCES = os.path.join(os.path.dirname(__file__), "data", "sources.json")
VALID   = {"scrape", "pdf", "feed", "search"}

with open(SOURCES, encoding="utf-8") as f:
    sources = json.load(f)

fixed = 0
for src in sources:
    for ep in src.get("endpoints", []):
        t = ep.get("agent", "")
        if t not in VALID:
            label = f'{src.get("kuerzel","?")} | {ep.get("url","")[:60]}'
            if t.startswith("http") or t in ("selector", "https"):
                ep["agent"] = "scrape"
                print(f"FIXED  scrape ← {repr(t):20s} | {label}")
            elif t == "":
                ep["agent"] = "scrape"
                print(f"FIXED  scrape ← (leer)              | {label}")
            else:
                print(f"SKIP   unbekannter Typ {repr(t):12s} | {label} — bitte manuell prüfen")
                continue
            fixed += 1

print(f"\n{fixed} Einträge korrigiert.")
if fixed > 0:
    with open(SOURCES, "w", encoding="utf-8") as f:
        json.dump(sources, f, ensure_ascii=False, indent=2)
    print("sources.json gespeichert.")
else:
    print("Keine Änderungen nötig.")
