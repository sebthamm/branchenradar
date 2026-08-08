import csv
import io
import json
import os
import uuid
import hashlib
import hmac
import subprocess
from datetime import datetime
from functools import wraps
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify, abort
)
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")

DATA_DIR      = os.path.join(os.path.dirname(__file__), "data")
SIGNALS_FILE  = os.path.join(DATA_DIR, "signals.json")
USERS_FILE    = os.path.join(DATA_DIR, "users.json")
ENTITIES_FILE = os.path.join(DATA_DIR, "entities.json")
SECTIONS_FILE = os.path.join(DATA_DIR, "sections.seed.json")  # sections are static
TODOS_FILE    = os.path.join(DATA_DIR, "todos.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
SOURCES_FILE             = os.path.join(DATA_DIR, "sources.json")
AGENT_CONFIGS_FILE       = os.path.join(DATA_DIR, "agent_configs.json")
SIGNAL_MATCHES_FILE      = os.path.join(DATA_DIR, "signal_matches.json")
SIGNAL_FINAL_FILE        = os.path.join(DATA_DIR, "signal_final.json")
SIGNAL_FINAL_ARCHIVE_DIR = os.path.join(DATA_DIR, "signal_final_archive")
AGENT_REPORTS_FILE       = os.path.join(DATA_DIR, "agent_reports.json")

DEFAULT_TODO_CATEGORIES = [
    "Abrechnung & Vergütung",
    "Compliance & Regulatorik",
    "Praxisorganisation",
    "IT & Digitalisierung",
    "Personal & Fortbildung",
]

def _init_data():
    """Copy seed files to data files on first run if data files don't exist."""
    import shutil
    for name in ("signals", "users", "entities", "todos", "sources"):
        target = os.path.join(DATA_DIR, f"{name}.json")
        seed   = os.path.join(DATA_DIR, f"{name}.seed.json")
        if not os.path.exists(target) and os.path.exists(seed):
            shutil.copy2(seed, target)
    # settings is a dict, not a list
    if not os.path.exists(SETTINGS_FILE):
        seed = os.path.join(DATA_DIR, "settings.seed.json")
        if os.path.exists(seed):
            shutil.copy2(seed, SETTINGS_FILE)
        else:
            _save(SETTINGS_FILE, {"default_todo_categories": DEFAULT_TODO_CATEGORIES})

_init_data()

DEPLOY_TOKEN = os.environ.get("DEPLOY_TOKEN", "")

STATUS_ORDER = ["action", "announced", "develop", "radar", "active"]
CATEGORIES = {
    "krankenkassen": "Krankenkassen & GKV",
    "digital":       "Digitalisierung & TI",
    "gesetze":       "Gesetze & Regulatorien",
    "personal":      "Personal & Tarife",
    "praxis":        "Praxismanagement",
}
STATUS_LABELS = {
    "action":    "Handlungsbedarf",
    "announced": "Angekündigt",
    "develop":   "In Entwicklung",
    "radar":     "Im Radar",
    "active":    "Verfügbar / In Kraft",
}
SIGNAL_PRIORITY_LABELS = {
    "muss":   "Muss",
    "sollte": "Sollte",
    "kann":   "Kann",
}
ENTWICKLUNGSSTAND_LABELS = {
    "beobachtung":    "Beobachtung",
    "entwurf":        "Entwurf",
    "angekuendigt":   "Angekündigt",
    "beschlossen":    "Beschlossen",
    "veroeffentlicht":"Veröffentlicht",
    "in_kraft":       "In Kraft",
    "abgeloest":      "Abgelöst",
}
HANDLUNGSZEITPUNKT_LABELS = {
    "jetzt":       "Jetzt handeln",
    "vorbereiten": "Bald handeln",
    "beobachten":  "Beobachten",
    "keine_aktion":"Keine Aktion",
}
AUFWAND_LABELS = {
    "unter_15":        "< 15 Min.",
    "15_60":           "15–60 Min.",
    "mehrere_stunden": "Mehrere Stunden",
}
SIGNAL_ROLLEN = [
    "Praxisleitung",
    "Ärzt:innen",
    "MFA / ZFA",
    "Abrechnung",
    "Praxismanagement",
    "Datenschutz",
    "QM",
]
ROLE_LABELS = {
    "superadmin": "Superadmin",
    "admin":      "Admin",
    "user":       "Nutzer",
}


# ── Data helpers ──────────────────────────────────────────────────────────────

def _load(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def _save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_signals():   return _load(SIGNALS_FILE)
def save_signals(d):  _save(SIGNALS_FILE, d)
def load_users():     return _load(USERS_FILE)
def save_users(d):    _save(USERS_FILE, d)
def load_entities():  return _load(ENTITIES_FILE)
def save_entities(d): _save(ENTITIES_FILE, d)
def load_sections():  return _load(SECTIONS_FILE)
def load_todos():     return _load(TODOS_FILE)
def save_todos(d):    _save(TODOS_FILE, d)
def load_sources():   return _load(SOURCES_FILE)
def save_sources(d):  _save(SOURCES_FILE, d)

_SEARCH_OUTPUT_FORMAT = (
    "## Ausgabeformat\n"
    "Für jedes gefundene Signal erstelle einen Eintrag mit folgenden Feldern:\n\n"
    "**Titel:** [Sprechender Action-Titel im Stil eines Managementpräsentations-Titels – prägnant, aktiv, handlungsorientiert, 8–12 Wörter]\n"
    "**Zusammenfassung:** [500–1000 Zeichen. Was passiert konkret, was bedeutet das für Gesundheitseinrichtungen?]\n"
    "**Datum:** [TT.MM.JJJJ – Datum der Veröffentlichung, des Beschlusses oder des Inkrafttretens]\n"
    "**Entwicklungsstand:** [Genau eines von: Beobachtung | Veröffentlicht | Beschlossen | In Kraft]\n"
    "**Handlungszeitpunkt:** [Genau eines von: Sofort | Kurzfristig (< 3 Monate) | Mittelfristig (3–12 Monate) | Langfristig (> 12 Monate) | Beobachten]\n"
    "**Quelle 1:** [Name der Quelle, z.B. G-BA, BMG, gematik, BZÄK]\n"
    "**Quellenlink 1:** [Direkte URL zum Originaldokument oder zur Meldung]\n"
    "**Agent:** [Exakter Name dieses Agenten: scrape | feed | pdf | search]\n\n"
    "Alle anderen Felder (Priorität, Kategorie, Fachbereich, Betroffene Rollen, Aufwand, Nächster Schritt) "
    "werden von den Such-Agenten nicht befüllt und bleiben leer.\n\n"
    "---\n\n"
    "## Crawl-Report\n"
    "Nach allen Signalen: Erstelle eine separate Tabelle im TSV-Format (Tab-getrennt) mit genau diesen 5 Spalten:\n"
    "Quelle\tAnzahl Signale\tHinweise/Probleme\tTimestamp\tAgent\n\n"
    "Führe **jede geprüfte Quelle** auf – auch solche ohne neue Signale oder mit technischen Problemen.\n"
    "– Anzahl Signale: Anzahl der in diesem Lauf für diese Quelle erstellten Signale (0 wenn keine)\n"
    "– Hinweise/Probleme: z.B. 'Seite nicht erreichbar', 'Login erforderlich', 'Kein neuer Inhalt', leer wenn ok\n"
    "– Timestamp: aktuelles Datum/Uhrzeit im Format JJMMDDhhmm (z.B. 2608061430)\n"
    "– Agent: exakter Name dieses Agenten (scrape | feed | pdf | search)"
)

_SEARCH_PERSONA_BASE = (
    "Du bist ein spezialisierter Such-Agent für den Branchenradar Gesundheitswesen. "
    "Deine Aufgabe ist es, relevante neue regulatorische Inhalte zu finden und als strukturierte Signale aufzubereiten. "
    "Ein Signal fasst ein regulatorisches Ereignis prägnant zusammen (500–1000 Zeichen) "
    "und gibt ihm einen sprechenden, handlungsorientierten Titel."
)

DEFAULT_AGENT_CONFIGS = {
    "output_format": _SEARCH_OUTPUT_FORMAT,
    "agents": {
        "scrape": {
            "label": "Scrape-Agent",
            "persona": _SEARCH_PERSONA_BASE + " Du spezialisierst dich auf öffentlich zugängliche Webseiten von Behörden, Verbänden und Institutionen.",
            "method": "Du rufst jede URL direkt ab (HTTP GET), analysierst den HTML-Inhalt und extrahierst relevante neue Dokumente, Meldungen oder Änderungen. Vergleiche mit bekannten Inhalten und melde nur tatsächlich neue Informationen.",
            "hint": "Achte besonders auf Neuigkeiten, Pressemitteilungen und aktuelle Meldungen. Ignoriere redaktionelle Artikel ohne konkreten regulatorischen Gehalt. Melde nur Inhalte, die seit der letzten Recherche neu erschienen sind."
        },
        "feed": {
            "label": "Feed-Agent",
            "persona": _SEARCH_PERSONA_BASE + " Du spezialisierst dich auf strukturierte Datenquellen: RSS-Feeds, Atom-Feeds, Newsletter-Archive und APIs.",
            "method": "Du liest RSS/Atom-Feeds, verarbeitest Newsletter-Inhalte oder rufst APIs ab und analysierst die zurückgegebenen Einträge auf Relevanz für Gesundheitseinrichtungen in Deutschland.",
            "hint": "Filtere nach Einträgen der letzten 7 Tage. Fokussiere auf Themen wie Abrechnung, Datenschutz, Hygiene, Qualitätsmanagement und regulatorische Änderungen. Ignoriere reine Veranstaltungshinweise ohne regulatorischen Inhalt."
        },
        "pdf": {
            "label": "PDF-Agent",
            "persona": _SEARCH_PERSONA_BASE + " Du spezialisierst dich auf verlinkte Dokumente (PDFs, Word-Dateien) von Quellen-Webseiten.",
            "method": "Du durchsuchst die Quellen-URLs nach verlinkten Dokumenten, lädst diese herunter und extrahierst relevante Inhalte aus PDFs und anderen Dokumentformaten.",
            "hint": "Priorisiere aktuelle Leitlinien, Rundschreiben, Beschlüsse und offizielle Bekanntmachungen. Achte auf Versionsnummern und Datumsangaben, um neue von bekannten Dokumenten zu unterscheiden. Ignoriere unveränderte Dokumente."
        },
        "search": {
            "label": "Search-Agent",
            "persona": _SEARCH_PERSONA_BASE + " Du spezialisierst dich auf Quellen mit Login-Pflicht, Datenbanken oder kostenpflichtigen Inhalten.",
            "method": "Du verwendest gespeicherte Zugangsdaten oder öffentliche Suchfunktionen, um in geschützten Bereichen oder Datenbanken nach neuen relevanten Inhalten zu suchen.",
            "hint": "Falls kein direkter Zugang möglich ist, suche nach öffentlichen Zusammenfassungen, Pressemitteilungen oder alternativen Zugangswegen zur gleichen Information."
        },
        "group": {
            "label": "Gruppierungs-Agent",
            "date_from": "", "date_to": "",
            "last_date_from": "", "last_date_to": "",
            "persona": (
                "Du bist ein redaktioneller Gruppierungs-Agent für den Branchenradar Gesundheitswesen. "
                "Du erhältst zwei Input-Datenbanken als Excel-Dateien: "
                "(1) neue Signale, die von den Such-Agenten in diesem Recherche-Zyklus gefunden wurden, und "
                "(2) alle Signale aus dem letzten Branchenradar-Bericht. "
                "Deine Aufgabe ist es, diese Datenbanken zu konsolidieren und ein aktualisiertes, bereinigtes Signal-Set zu erstellen."
            ),
            "method": (
                "Schritt 1 – Neue Signale zusammenführen:\n"
                "Prüfe alle neuen Signale aus den Such-Agenten auf inhaltliche Überschneidungen. "
                "Signale, die dasselbe regulatorische Ereignis oder denselben Vorgang beschreiben, werden zu einem Signal zusammengefasst. "
                "Das zusammengeführte Signal erhält alle Quellen, Quellenlinks und Daten der Einzelsignale "
                "(Quelle 1 / Quellenlink 1, Quelle 2 / Quellenlink 2, Quelle 3 / Quellenlink 3 etc.). "
                "Der Titel und die Zusammenfassung werden redaktionell zusammengeführt.\n\n"
                "Schritt 2 – Abgleich mit bestehendem Reporting:\n"
                "Vergleiche die konsolidierten neuen Signale mit den Signalen aus dem letzten Branchenradar-Bericht. "
                "Entscheide für jedes neue Signal:\n"
                "– Ist es ein Update, eine Ergänzung oder Konkretisierung eines bestehenden Signals? "
                "→ Status: UPDATE. Hänge die neuen Informationen als separaten Absatz an die bestehende Zusammenfassung an, "
                "versehen mit dem Datum der Neuerung im Format [TT.MM.JJJJ]: …\n"
                "– Ist es ein völlig neues Thema, das bisher nicht im Branchenradar war? "
                "→ Status: NEU.\n"
                "– Bestehende Signale aus dem alten Bericht ohne jede Neuerung erhalten Status: leer (unverändert). "
                "Sie werden unverändert in die Ausgabe übernommen."
            ),
            "hint": (
                "Nutze semantisches Verständnis für den Abgleich – nicht nur Stichwortübereinstimmungen. "
                "Verschiedene Meldungen zur selben Gesetzgebung, zum selben Beschluss oder zum selben Verfahren gehören zusammen. "
                "Achte besonders auf Datumsangaben: neuere Meldungen zum gleichen Thema sind Update-Kandidaten. "
                "Im Zweifel lieber zusammenführen als doppelt aufführen. "
                "Behalte die Originalzusammenfassungen bestehender Signale vollständig – ergänze nur, lösche nicht."
            ),
            "output_format": (
                "## Ausgabeformat – Signale (MATCH)\n\n"
                "Erstelle eine tabellarische Ausgabe im folgenden Format. Jede Zeile beschreibt eine Gruppe zusammengehöriger Signale.\n\n"
                "**Spalten der MATCH-Tabelle:**\n"
                "- Spalte 1 (sig1): ID des primären Signals aus Signale (RAW) — das Signal, das die Gruppe anführt\n"
                "- Spalte 2 (sig2): ID eines weiteren Signals, das mit sig1 zusammengeführt wird (leer = keine weiteren)\n"
                "- Spalte 3 (sig2_type): NEW wenn sig2 ein neu recherchiertes Signal ist | OLD wenn sig2 aus dem letzten Reporting stammt\n"
                "- Spalte 4 (sig3): ID eines dritten Signals (optional)\n"
                "- Spalte 5 (sig3_type): NEW | OLD\n"
                "- Spalte 6 (sig4): ID eines vierten Signals (optional)\n"
                "- Spalte 7 (sig4_type): NEW | OLD\n"
                "- Spalte 8 (sig5): ID eines fünften Signals (optional)\n"
                "- Spalte 9 (sig5_type): NEW | OLD\n\n"
                "**Signal-IDs** haben das Format JJMMDDXXXXX (z.B. 260806000042). "
                "Jedes Signal aus Signale (RAW) hat eine solche ID. Bestehende Signale aus dem letzten Reporting haben ebenfalls IDs in diesem Format.\n\n"
                "**Beispielzeile:**\n"
                "sig1=260806000017 | sig2=260806000031 | sig2_type=NEW | sig3=260801000008 | sig3_type=OLD\n\n"
                "Signale ohne Überschneidung mit anderen Signalen erscheinen als eigenständige Zeilen mit nur sig1 befüllt (alle anderen Spalten leer).\n\n"
                "Nach der MATCH-Tabelle: Erstelle zusätzlich eine vollständige Signal-Liste für die redaktionelle Weiterbearbeitung:\n\n"
                "**Status:** NEU | UPDATE | (leer = unverändert)\n"
                "**Titel:** [Sprechender Action-Titel]\n"
                "**Zusammenfassung:** [Bestehende Zusammenfassung. Bei UPDATE: Neuer Absatz mit [TT.MM.JJJJ]: Neue Informationen]\n"
                "**Datum:** [Datum des Signals bzw. aktuellstes Datum bei Updates]\n"
                "**Entwicklungsstand:** [Beobachtung | Veröffentlicht | Beschlossen | In Kraft]\n"
                "**Quelle 1:** [Name] | **Quellenlink 1:** [URL]\n"
                "**Quelle 2:** [Name] | **Quellenlink 2:** [URL] (falls vorhanden)\n"
                "**Quelle 3:** [Name] | **Quellenlink 3:** [URL] (falls vorhanden)\n\n"
                "---"
            )
        },
        "bewertung": {
            "label": "Bewertungs-Agent",
            "persona": (
                "Du bist ein redaktioneller Bewertungs-Agent für den Branchenradar Gesundheitswesen. "
                "Du erhältst drei Input-Datenbanken: "
                "(1) Signale (RAW) – neue, von den Such-Agenten recherchierte Rohdaten, "
                "(2) Signale (FINAL) – die konsolidierten Signale des letzten Branchenradar-Reportings, "
                "(3) Signale (MATCH) – die Zuordnungstabelle des Gruppierungs-Agenten, die festlegt, "
                "welche RAW-Signale zusammengehören und ob sie NEU oder ein UPDATE zu einem bestehenden FINAL-Signal sind. "
                "Deine Aufgabe ist es, auf dieser Basis den vollständig angereicherten, neuen Signaldatensatz zu erstellen, "
                "der als nächstes Signale (FINAL) gespeichert wird."
            ),
            "method": (
                "Schritt 1 – Signale zusammenführen:\n"
                "Nutze die MATCH-Tabelle als Fahrplan. Für jede MATCH-Gruppe:\n"
                "– Kombiniere alle referenzierten RAW-Signale (NEW) zu einem inhaltlich kohärenten Signal. "
                "Führe Titel, Zusammenfassung und Quellenangaben zusammen.\n"
                "– Falls die Gruppe auch OLD-Signale enthält (aus FINAL), erweitere das bestehende FINAL-Signal "
                "um die neuen Informationen. Hänge neue Absätze mit Datumsmarkierung [TT.MM.JJJJ] an.\n"
                "– Bestehende FINAL-Signale ohne jede MATCH-Verbindung werden unverändert übernommen.\n\n"
                "Schritt 2 – Eigenständige neue Signale:\n"
                "RAW-Signale, die in keiner MATCH-Gruppe auftauchen, sind völlig neue Themen. "
                "Übernimm sie direkt als neue Signale in den Output.\n\n"
                "Schritt 3 – Anreicherung aller Signale:\n"
                "Für jedes Signal – ob neu, aktualisiert oder unverändert – vergib oder überprüfe:\n"
                "– **Priorität:** MUSS (unmittelbarer Handlungsbedarf, rechtlich verpflichtend oder hohe finanzielle Auswirkung), "
                "SOLLTE (relevante Entwicklung, Handlung empfohlen), "
                "KANN (interessant zur Beobachtung, noch kein konkreter Handlungsbedarf)\n"
                "– **Handlungsempfehlung:** Ein konkreter, umsetzbarer Handlungsschritt im Imperativ (z.B. 'TI-Anbindung bis Q2 prüfen und beauftragen'). "
                "Nicht allgemein, sondern spezifisch für Gesundheitseinrichtungen.\n"
                "– **Umsetzungszeit:** Realistische Einschätzung des Zeitaufwands für eine typische Praxis "
                "(z.B. '1–2 Stunden', '1 Tag', '1–2 Wochen', '1–3 Monate').\n"
                "– **Betroffene Rollen:** Wer in der Praxis ist konkret betroffen? "
                "Auswahl aus: Praxisleitung, Ärzt:innen, MFA / ZFA, Abrechnung, Praxismanagement, Datenschutz, QM\n"
                "– **Handlungszeitpunkt:** Sofort | Kurzfristig (< 3 Monate) | Mittelfristig (3–12 Monate) | Langfristig (> 12 Monate) | Beobachten\n"
                "– **Kategorie:** Krankenkassen & GKV | Digitalisierung & TI | Gesetze & Regulatorien | Personal & Tarife | Praxismanagement\n"
                "– **Reporting-Status:** NEU | UPDATE | (leer = unverändert)"
            ),
            "hint": (
                "Sei bei der Prioritätsvergabe streng: MUSS ist nur für wirklich zwingende, "
                "fristgebundene oder sanktionsbewehrte Verpflichtungen. Nicht jede neue Entwicklung ist ein MUSS. "
                "Die Handlungsempfehlung soll dem Praxisinhaber oder -manager sofort klar machen, was jetzt konkret zu tun ist – "
                "keine vagen Formulierungen wie 'informieren' oder 'beobachten' bei MUSS-Signalen. "
                "Betroffene Rollen sparsam vergeben – nur die Rollen, die tatsächlich direkt handeln müssen."
            ),
            "output_format": (
                "## Ausgabeformat – Signale (FINAL)\n\n"
                "Erstelle eine vollständige Liste aller Signale im folgenden Format. "
                "Jedes Signal auf einer neuen Sektion, getrennt durch ---\n\n"
                "**ID:** [Übernehmen aus RAW/FINAL – bei vollständig neuen Signalen ohne MATCH: neue ID aus RAW]\n"
                "**Reporting-Status:** NEU | UPDATE | (leer)\n"
                "**Priorität:** MUSS | SOLLTE | KANN\n"
                "**Titel:** [Sprechender Action-Titel]\n"
                "**Zusammenfassung:** [Vollständige Zusammenfassung. Bei UPDATE: bestehende Zusammenfassung + [TT.MM.JJJJ]: neue Informationen]\n"
                "**Kategorie:** [Krankenkassen & GKV | Digitalisierung & TI | Gesetze & Regulatorien | Personal & Tarife | Praxismanagement]\n"
                "**Entwicklungsstand:** [Beobachtung | Veröffentlicht | Beschlossen | In Kraft]\n"
                "**Handlungsempfehlung:** [Konkreter Handlungsschritt im Imperativ]\n"
                "**Umsetzungszeit:** [z.B. 1–2 Stunden | 1 Tag | 1–2 Wochen | 1–3 Monate]\n"
                "**Betroffene Rollen:** [Kommagetrennte Liste aus: Praxisleitung, Ärzt:innen, MFA / ZFA, Abrechnung, Praxismanagement, Datenschutz, QM]\n"
                "**Handlungszeitpunkt:** [Sofort | Kurzfristig (< 3 Monate) | Mittelfristig (3–12 Monate) | Langfristig (> 12 Monate) | Beobachten]\n"
                "**Datum:** [Datum des Signals / aktuellstes Datum bei Updates]\n"
                "**Quelle 1:** [Name] | **Quellenlink 1:** [URL]\n"
                "**Quelle 2:** [Name] | **Quellenlink 2:** [URL] (falls vorhanden)\n"
                "**Quelle 3:** [Name] | **Quellenlink 3:** [URL] (falls vorhanden)\n\n"
                "---"
            )
        }
    }
}

def load_agent_configs():
    import copy
    cfg = copy.deepcopy(DEFAULT_AGENT_CONFIGS)
    if not os.path.exists(AGENT_CONFIGS_FILE):
        return cfg
    data = _load(AGENT_CONFIGS_FILE)
    # Only override with saved value if it's non-empty
    if data.get("output_format", "").strip():
        cfg["output_format"] = data["output_format"]
    for key in cfg["agents"]:
        saved = data.get("agents", {}).get(key, {})
        for field in ("persona", "method", "hint", "output_format"):
            if saved.get(field, "").strip():
                cfg["agents"][key][field] = saved[field]
        for field in ("date_from", "date_to", "last_date_from", "last_date_to"):
            if field in saved:
                cfg["agents"][key][field] = saved[field]
    return cfg

def save_agent_configs(d): _save(AGENT_CONFIGS_FILE, d)

def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        return {"default_todo_categories": DEFAULT_TODO_CATEGORIES}
    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_settings(d): _save(SETTINGS_FILE, d)

def load_agent_reports():
    if not os.path.exists(AGENT_REPORTS_FILE):
        return []
    return _load(AGENT_REPORTS_FILE) or []

def save_agent_reports(data): _save(AGENT_REPORTS_FILE, data)

def load_signal_matches():
    if not os.path.exists(SIGNAL_MATCHES_FILE):
        return []
    return _load(SIGNAL_MATCHES_FILE) or []

def save_signal_matches(data): _save(SIGNAL_MATCHES_FILE, data)

def load_signal_final():
    if not os.path.exists(SIGNAL_FINAL_FILE):
        return {"timestamp": None, "signals": []}
    data = _load(SIGNAL_FINAL_FILE)
    if isinstance(data, list):
        return {"timestamp": None, "signals": data}
    return data

def save_signal_final(signals, timestamp):
    _save(SIGNAL_FINAL_FILE, {"timestamp": timestamp, "signals": signals})

def archive_signal_final():
    if not os.path.exists(SIGNAL_FINAL_FILE):
        return
    os.makedirs(SIGNAL_FINAL_ARCHIVE_DIR, exist_ok=True)
    data = load_signal_final()
    ts = data.get("timestamp") or datetime.now().strftime("%y%m%d%H%M")
    archive_path = os.path.join(SIGNAL_FINAL_ARCHIVE_DIR, f"{ts}.json")
    _save(archive_path, data)

def list_signal_final_archives():
    if not os.path.exists(SIGNAL_FINAL_ARCHIVE_DIR):
        return []
    files = sorted(
        [f[:-5] for f in os.listdir(SIGNAL_FINAL_ARCHIVE_DIR) if f.endswith(".json")],
        reverse=True
    )
    return files

def load_signal_final_archive(ts):
    path = os.path.join(SIGNAL_FINAL_ARCHIVE_DIR, f"{ts}.json")
    if not os.path.exists(path):
        return None
    return _load(path)

def next_signal_raw_id():
    cfg = load_settings()
    counter = cfg.get("signal_raw_counter", 0) + 1
    cfg["signal_raw_counter"] = counter
    save_settings(cfg)
    return f"{datetime.now().strftime('%y%m%d')}{counter:05d}"

def entity_todo_categories(entity_id):
    if not entity_id:
        return DEFAULT_TODO_CATEGORIES
    e = get_entity(entity_id)
    if e:
        return e.get("todo_categories", DEFAULT_TODO_CATEGORIES)
    return DEFAULT_TODO_CATEGORIES

def sections_by_id():
    return {s["id"]: s for s in load_sections()}

def get_entity(eid):
    return next((e for e in load_entities() if e["id"] == eid), None)

def get_user(uid):
    return next((u for u in load_users() if u["id"] == uid), None)

def _hash(pw): return hashlib.sha256(pw.encode()).hexdigest()


# ── Auth decorators ───────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated

def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not session.get("user_id"):
                return redirect(url_for("login", next=request.path))
            if session.get("role") not in roles:
                abort(403)
            return f(*args, **kwargs)
        return decorated
    return decorator


# ── Context processor ────────────────────────────────────────────────────────

@app.context_processor
def inject_globals():
    uid  = session.get("user_id")
    eid  = session.get("entity_id")
    role = session.get("role")

    entity_users = []
    if role in ("admin", "superadmin") and eid:
        entity_users = [u for u in load_users() if u.get("entity_id") == eid]

    badge = 0
    if uid:
        todos = load_todos()
        if role in ("admin", "superadmin"):
            # open todos for entity + unread done-notifications
            badge = sum(
                1 for t in todos
                if t.get("entity_id") == eid and (
                    (not t.get("done")) or
                    (t.get("done") and t.get("assigned_by") == uid and not t.get("admin_read_at"))
                )
            )
        else:
            # user: unread assigned todos
            badge = sum(1 for t in todos if t.get("assigned_to") == uid and not t.get("assignee_read_at"))

    return {
        "todo_categories": entity_todo_categories(eid),
        "pending_todos":   badge,
        "entity_users":    entity_users,
    }


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        users = load_users()
        user = next((u for u in users if u["username"] == username), None)
        if user and hmac.compare_digest(user["pass_hash"], _hash(password)):
            session["user_id"]   = user["id"]
            session["username"]  = user["username"]
            session["role"]      = user["role"]
            session["name"]      = user.get("name", username)
            session["entity_id"] = user.get("entity_id")
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Benutzername oder Passwort falsch.", "error")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("dashboard"))


# ── Public dashboard ──────────────────────────────────────────────────────────

@app.route("/")
@login_required
def dashboard():
    all_signals = load_signals()
    sections    = load_sections()
    sec_map     = {s["id"]: s for s in sections}

    # Determine which sections the current user's entity has access to
    entity_id   = session.get("entity_id")
    role        = session.get("role")
    if role == "superadmin" or not entity_id:
        allowed_section_ids = None  # sees everything
    else:
        entity = get_entity(entity_id)
        allowed_section_ids = set(entity.get("section_ids", [])) if entity else set()

    # Filter signals to allowed sections
    if allowed_section_ids is not None:
        signals = [s for s in all_signals
                   if any(sid in allowed_section_ids for sid in s.get("section_ids", []))]
        visible_sections = [s for s in sections if s["id"] in allowed_section_ids]
    else:
        signals = all_signals
        visible_sections = sections

    # For "user" role: additionally filter by team categories
    if role == "user" and entity_id:
        entity = get_entity(entity_id)
        if entity:
            uid = session.get("user_id")
            user_teams = [t for t in entity.get("teams", []) if uid in t.get("member_ids", [])]
            if user_teams:
                team_cats = set()
                for tm in user_teams:
                    team_cats.update(tm.get("category_ids", []))
                if team_cats:
                    signals = [s for s in signals if s.get("category") in team_cats]

    prio_order = ["muss", "sollte", "kann"]
    def sort_key(s):
        try:   pi = prio_order.index(s.get("priority", "sollte"))
        except ValueError: pi = 1
        return (pi, s.get("date", ""))
    signals.sort(key=sort_key)

    prio_counts = {p: 0 for p in prio_order}
    for sig in signals:
        p = sig.get("priority", "sollte")
        if p in prio_counts:
            prio_counts[p] += 1

    sec_counts = {}
    for sig in signals:
        for sid in sig.get("section_ids", []):
            sec_counts[sid] = sec_counts.get(sid, 0) + 1

    return render_template(
        "dashboard.html",
        signals=signals, prio_counts=prio_counts,
        categories=CATEGORIES, status_labels=STATUS_LABELS,
        priority_labels=SIGNAL_PRIORITY_LABELS,
        entwicklungsstand_labels=ENTWICKLUNGSSTAND_LABELS,
        handlungszeitpunkt_labels=HANDLUNGSZEITPUNKT_LABELS,
        aufwand_labels=AUFWAND_LABELS,
        sections=visible_sections, sec_map=sec_map, sec_counts=sec_counts,
        now=datetime.now().strftime("%d. %B %Y, %H:%M"),
        total=len(signals),
    )


# ── Superadmin: Entity management ─────────────────────────────────────────────

@app.route("/superadmin")
@role_required("superadmin")
def sa_dashboard():
    entities  = load_entities()
    users     = load_users()
    sections  = load_sections()
    sec_map   = {s["id"]: s for s in sections}
    counts = {}
    for u in users:
        eid = u.get("entity_id")
        if eid:
            counts[eid] = counts.get(eid, 0) + 1
    return render_template("sa_dashboard.html",
        entities=entities, user_counts=counts, users=users,
        sections=sections, sec_map=sec_map, role_labels=ROLE_LABELS)

@app.route("/superadmin/entities/<eid>/edit", methods=["GET", "POST"])
@role_required("superadmin")
def sa_entity_edit(eid):
    entities = load_entities()
    entity = next((e for e in entities if e["id"] == eid), None)
    if not entity:
        abort(404)
    if request.method == "POST":
        entity["name"]        = request.form.get("name", "").strip()
        entity["section_ids"] = request.form.getlist("section_ids")
        save_entities(entities)
        flash("Entity aktualisiert.", "success")
        return redirect(url_for("sa_dashboard"))
    return render_template("sa_entity_form.html", entity=entity,
        sections=load_sections())

@app.route("/superadmin/entities/new", methods=["GET", "POST"])
@role_required("superadmin")
def sa_entity_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Name ist erforderlich.", "error")
            return render_template("sa_entity_form.html", entity=None, sections=load_sections())
        entities = load_entities()
        cfg = load_settings()
        default_cats = cfg.get("default_todo_categories", DEFAULT_TODO_CATEGORIES)
        new_entity = {
            "id":              str(uuid.uuid4()),
            "name":            name,
            "created_at":      datetime.now().strftime("%Y-%m-%d"),
            "section_ids":     request.form.getlist("section_ids"),
            "todo_categories": default_cats,
            "teams": [
                {"id": str(uuid.uuid4()), "name": tn, "member_ids": [], "category_ids": []}
                for tn in cfg.get("default_team_names", ["Team Abrechnung", "Team Personal"])
            ],
        }
        entities.append(new_entity)
        save_entities(entities)

        # Optionally create admin user right away
        admin_username = request.form.get("admin_username", "").strip()
        admin_password = request.form.get("admin_password", "").strip()
        admin_name     = request.form.get("admin_name", "").strip()
        if admin_username and admin_password:
            users = load_users()
            if any(u["username"] == admin_username for u in users):
                flash(f"Entity '{name}' angelegt. Benutzername '{admin_username}' existiert bereits — Admin manuell anlegen.", "error")
            else:
                users.append({
                    "id":        str(uuid.uuid4()),
                    "username":  admin_username,
                    "pass_hash": _hash(admin_password),
                    "role":      "admin",
                    "entity_id": new_entity["id"],
                    "name":      admin_name or admin_username,
                })
                save_users(users)
                flash(f"Entity '{name}' und Admin '{admin_username}' angelegt.", "success")
        else:
            flash(f"Entity '{name}' angelegt. Admin noch hinzufügen.", "success")
        return redirect(url_for("sa_dashboard"))
    return render_template("sa_entity_form.html", entity=None, sections=load_sections())

@app.route("/superadmin/entities/<eid>/delete", methods=["POST"])
@role_required("superadmin")
def sa_entity_delete(eid):
    entities = [e for e in load_entities() if e["id"] != eid]
    save_entities(entities)
    # Remove users of this entity
    users = [u for u in load_users() if u.get("entity_id") != eid]
    save_users(users)
    flash("Entity und zugehörige Nutzer gelöscht.", "success")
    return redirect(url_for("sa_dashboard"))

@app.route("/superadmin/users/new", methods=["GET", "POST"])
@role_required("superadmin")
def sa_user_new():
    if request.method == "POST":
        return _create_user(request.form, redirect_to=url_for("sa_dashboard"))
    entities = load_entities()
    return render_template("user_form.html", user=None, entities=entities,
        role_labels=ROLE_LABELS, action=url_for("sa_user_new"),
        show_all_roles=True)

@app.route("/superadmin/users/<uid>/delete", methods=["POST"])
@role_required("superadmin")
def sa_user_delete(uid):
    users = [u for u in load_users() if u["id"] != uid]
    save_users(users)
    flash("Nutzer gelöscht.", "success")
    return redirect(url_for("sa_dashboard"))

@app.route("/superadmin/users/<uid>/edit", methods=["GET", "POST"])
@role_required("superadmin")
def sa_user_edit(uid):
    users = load_users()
    user = next((u for u in users if u["id"] == uid), None)
    if not user:
        abort(404)
    if request.method == "POST":
        user["name"]      = request.form.get("name", "").strip()
        user["username"]  = request.form.get("username", "").strip()
        user["role"]      = request.form.get("role", "user")
        user["entity_id"] = request.form.get("entity_id") or None
        pw = request.form.get("password", "").strip()
        if pw:
            user["pass_hash"] = _hash(pw)
        save_users(users)
        flash("Nutzer aktualisiert.", "success")
        return redirect(url_for("sa_dashboard"))
    entities = load_entities()
    return render_template("user_form.html", user=user, entities=entities,
        role_labels=ROLE_LABELS, action=url_for("sa_user_edit", uid=uid),
        show_all_roles=True)


# ── Admin: User management within entity ──────────────────────────────────────

@app.route("/admin")
@role_required("admin", "superadmin")
def admin_dashboard():
    # Superadmin → redirect to sa_dashboard
    if session["role"] == "superadmin":
        return redirect(url_for("sa_dashboard"))
    entity_id = session["entity_id"]
    entity = get_entity(entity_id)
    users = [u for u in load_users() if u.get("entity_id") == entity_id]
    return render_template("admin_users.html",
        entity=entity, users=users, role_labels=ROLE_LABELS)

@app.route("/admin/users/new", methods=["GET", "POST"])
@role_required("admin", "superadmin")
def admin_user_new():
    entity_id = session["entity_id"]
    if request.method == "POST":
        form = request.form.copy()
        # Admin can only create users in their own entity
        return _create_user(form, redirect_to=url_for("admin_dashboard"),
                            force_entity=entity_id, force_role="user")
    entity = get_entity(entity_id)
    return render_template("user_form.html", user=None,
        entities=[entity] if entity else [],
        role_labels={"user": "Nutzer"},
        action=url_for("admin_user_new"), show_all_roles=False)

@app.route("/admin/users/<uid>/delete", methods=["POST"])
@role_required("admin", "superadmin")
def admin_user_delete(uid):
    entity_id = session["entity_id"]
    users = load_users()
    target = next((u for u in users if u["id"] == uid), None)
    if not target or target.get("entity_id") != entity_id:
        abort(403)
    users = [u for u in users if u["id"] != uid]
    save_users(users)
    flash("Nutzer entfernt.", "success")
    return redirect(url_for("admin_dashboard"))


# ── Signal management (admin + superadmin) ────────────────────────────────────

@app.route("/admin/signals/raw")
@role_required("admin", "superadmin")
def signal_list_raw():
    return signal_list()

@app.route("/admin/signals")
@role_required("admin", "superadmin")
def signal_list():
    signals = load_signals()
    signals.sort(key=lambda s: s.get("date", ""), reverse=True)
    sections = load_sections()
    sec_map  = {s["id"]: s for s in sections}
    return render_template("admin_signals.html",
        signals=signals, categories=CATEGORIES, status_labels=STATUS_LABELS,
        priority_labels=SIGNAL_PRIORITY_LABELS,
        entwicklungsstand_labels=ENTWICKLUNGSSTAND_LABELS,
        handlungszeitpunkt_labels=HANDLUNGSZEITPUNKT_LABELS,
        aufwand_labels=AUFWAND_LABELS,
        signal_rollen=SIGNAL_ROLLEN,
        sections=sections, sec_map=sec_map)

@app.route("/admin/signals/new", methods=["GET", "POST"])
@role_required("admin", "superadmin")
def signal_new():
    if request.method == "POST":
        sig = _signal_from_form(request.form)
        signals = load_signals()
        signals.append(sig)
        save_signals(signals)
        flash("Signal gespeichert.", "success")
        return redirect(url_for("signal_list"))
    return render_template("signal_form.html", signal=None,
        categories=CATEGORIES, status_labels=STATUS_LABELS,
        priority_labels=SIGNAL_PRIORITY_LABELS,
        entwicklungsstand_labels=ENTWICKLUNGSSTAND_LABELS,
        handlungszeitpunkt_labels=HANDLUNGSZEITPUNKT_LABELS,
        aufwand_labels=AUFWAND_LABELS,
        signal_rollen=SIGNAL_ROLLEN,
        sections=load_sections(), action=url_for("signal_new"))

@app.route("/admin/signals/<sig_id>/edit", methods=["GET", "POST"])
@role_required("admin", "superadmin")
def signal_edit(sig_id):
    signals = load_signals()
    sig = next((s for s in signals if s["id"] == sig_id), None)
    if not sig:
        abort(404)
    if request.method == "POST":
        updated = _signal_from_form(request.form, existing_id=sig_id)
        for i, s in enumerate(signals):
            if s["id"] == sig_id:
                signals[i] = updated
                break
        save_signals(signals)
        flash("Signal aktualisiert.", "success")
        return redirect(url_for("signal_list"))
    return render_template("signal_form.html", signal=sig,
        categories=CATEGORIES, status_labels=STATUS_LABELS,
        priority_labels=SIGNAL_PRIORITY_LABELS,
        entwicklungsstand_labels=ENTWICKLUNGSSTAND_LABELS,
        handlungszeitpunkt_labels=HANDLUNGSZEITPUNKT_LABELS,
        aufwand_labels=AUFWAND_LABELS,
        signal_rollen=SIGNAL_ROLLEN,
        sections=load_sections(), action=url_for("signal_edit", sig_id=sig_id))

@app.route("/admin/signals/<sig_id>/delete", methods=["POST"])
@role_required("admin", "superadmin")
def signal_delete(sig_id):
    signals = [s for s in load_signals() if s["id"] != sig_id]
    save_signals(signals)
    flash("Signal gelöscht.", "success")
    return redirect(url_for("signal_list"))

# ── ToDos ─────────────────────────────────────────────────────────────────────

@app.route("/todos")
@login_required
def todos():
    uid  = session.get("user_id")
    eid  = session.get("entity_id")
    role = session.get("role")
    all_todos = load_todos()

    if role in ("admin", "superadmin"):
        my_todos = [t for t in all_todos if t.get("entity_id") == eid]
        # Mark done-notifications as read for this admin
        changed = False
        for t in all_todos:
            if t.get("assigned_by") == uid and t.get("done") and not t.get("admin_read_at"):
                t["admin_read_at"] = datetime.now().isoformat()
                changed = True
        if changed:
            save_todos(all_todos)
    else:
        my_todos = [t for t in all_todos if t.get("assigned_to") == uid]
        # Mark assignee unread as read
        changed = False
        for t in all_todos:
            if t.get("assigned_to") == uid and not t.get("assignee_read_at"):
                t["assignee_read_at"] = datetime.now().isoformat()
                changed = True
        if changed:
            save_todos(all_todos)

    sig_map    = {s["id"]: s for s in load_signals()}
    user_map   = {u["id"]: u for u in load_users()}
    cats       = entity_todo_categories(eid)
    today      = datetime.now().strftime("%Y-%m-%d")
    return render_template("todos.html",
        todos=my_todos, sig_map=sig_map, user_map=user_map,
        categories=cats, today=today, status_labels=STATUS_LABELS)

@app.route("/todos/new", methods=["POST"])
@role_required("admin", "superadmin")
def todo_new():
    uid = session.get("user_id")
    eid = session.get("entity_id")
    assigned_to = request.form.get("assigned_to", "").strip() or None
    todo = {
        "id":              str(uuid.uuid4()),
        "signal_id":       request.form.get("signal_id", ""),
        "signal_title":    request.form.get("signal_title", ""),
        "entity_id":       eid,
        "category":        request.form.get("category", ""),
        "deadline":        request.form.get("deadline", ""),
        "comment":         request.form.get("comment", "").strip(),
        "done":            False,
        "done_at":         None,
        "done_comment":    "",
        "done_by":         None,
        "assigned_to":     assigned_to,
        "assigned_by":     uid if assigned_to else None,
        "assigned_at":     datetime.now().isoformat() if assigned_to else None,
        "assignee_read_at": None,
        "admin_read_at":   None,
        "created_by":      uid,
        "created_at":      datetime.now().isoformat(),
    }
    todos = load_todos()
    todos.append(todo)
    save_todos(todos)
    flash("ToDo angelegt.", "success")
    return redirect(request.form.get("next") or url_for("todos"))

@app.route("/todos/<todo_id>/assign", methods=["POST"])
@role_required("admin", "superadmin")
def todo_assign(todo_id):
    uid = session.get("user_id")
    assigned_to = request.form.get("assigned_to", "").strip() or None
    todos = load_todos()
    for t in todos:
        if t["id"] == todo_id:
            t["assigned_to"]      = assigned_to
            t["assigned_by"]      = uid if assigned_to else None
            t["assigned_at"]      = datetime.now().isoformat() if assigned_to else None
            t["assignee_read_at"] = None
            break
    save_todos(todos)
    return redirect(url_for("todos"))

@app.route("/todos/<todo_id>/complete", methods=["POST"])
@login_required
def todo_complete(todo_id):
    uid = session.get("user_id")
    todos = load_todos()
    for t in todos:
        if t["id"] == todo_id:
            t["done"]          = True
            t["done_at"]       = datetime.now().isoformat()
            t["done_comment"]  = request.form.get("done_comment", "").strip()
            t["done_by"]       = uid
            t["admin_read_at"] = None  # admin gets notified
            break
    save_todos(todos)
    return redirect(url_for("todos"))

@app.route("/todos/<todo_id>/reopen", methods=["POST"])
@role_required("admin", "superadmin")
def todo_reopen(todo_id):
    todos = load_todos()
    for t in todos:
        if t["id"] == todo_id:
            t["done"]         = False
            t["done_at"]      = None
            t["done_comment"] = ""
            t["done_by"]      = None
            t["admin_read_at"] = None
            break
    save_todos(todos)
    return redirect(url_for("todos"))

@app.route("/todos/<todo_id>/delete", methods=["POST"])
@role_required("admin", "superadmin")
def todo_delete(todo_id):
    todos = [t for t in load_todos() if t["id"] != todo_id]
    save_todos(todos)
    return redirect(url_for("todos"))


# ── Settings ──────────────────────────────────────────────────────────────────

@app.route("/settings", methods=["GET", "POST"])
@role_required("admin", "superadmin")
def settings():
    eid = session.get("entity_id")
    entities = load_entities()
    entity = next((e for e in entities if e["id"] == eid), None)
    if not entity:
        abort(404)
    if request.method == "POST":
        action = request.form.get("action", "categories")
        if action == "categories":
            cats = [c.strip() for c in request.form.getlist("categories") if c.strip()]
            entity["todo_categories"] = cats
        elif action == "teams":
            teams = []
            i = 0
            while request.form.get(f"team_{i}_name") is not None:
                name    = request.form.get(f"team_{i}_name", "").strip()
                tid     = request.form.get(f"team_{i}_id") or str(uuid.uuid4())
                members = request.form.getlist(f"team_{i}_members")
                cats_t  = request.form.getlist(f"team_{i}_cats")
                if name:
                    teams.append({"id": tid, "name": name, "member_ids": members, "category_ids": cats_t})
                i += 1
            entity["teams"] = teams
        save_entities(entities)
        flash("Einstellungen gespeichert.", "success")
        return redirect(url_for("settings"))
    cats       = entity.get("todo_categories", DEFAULT_TODO_CATEGORIES)
    teams      = entity.get("teams", [])
    all_users  = [u for u in load_users() if u.get("entity_id") == eid]
    return render_template("settings.html", entity=entity, categories=cats,
        teams=teams, entity_users=all_users, signal_categories=CATEGORIES)

@app.route("/superadmin/functions")
@role_required("superadmin")
def sa_functions():
    return render_template("sa_functions.html",
        now=datetime.now().strftime("%d. %B %Y"))

@app.route("/superadmin/logbook")
@role_required("superadmin")
def sa_logbook():
    return render_template("sa_logbook.html")

@app.route("/superadmin/settings", methods=["GET", "POST"])
@role_required("superadmin")
def sa_settings():
    cfg = load_settings()
    if request.method == "POST":
        section = request.args.get("section", "todo")
        if section == "teams":
            teams = [t.strip() for t in request.form.getlist("team_names") if t.strip()]
            cfg["default_team_names"] = teams
            save_settings(cfg)
            flash("Standard-Teams gespeichert.", "success")
        else:
            cats = [c.strip() for c in request.form.getlist("categories") if c.strip()]
            cfg["default_todo_categories"] = cats
            save_settings(cfg)
            flash("Standard-Kategorien gespeichert.", "success")
        return redirect(url_for("sa_settings"))
    cats  = cfg.get("default_todo_categories", DEFAULT_TODO_CATEGORIES)
    teams = cfg.get("default_team_names", ["Team Abrechnung", "Team Personal"])
    return render_template("sa_settings.html", categories=cats, default_teams=teams)


@app.route("/admin/signals/import", methods=["GET", "POST"])
@role_required("admin", "superadmin")
def signal_import():
    sections = load_sections()
    if request.method == "POST":
        raw = ""
        f = request.files.get("file")
        if f and f.filename:
            raw = f.read().decode("utf-8-sig")
        else:
            raw = request.form.get("csv_text", "").strip()
        if not raw:
            flash("Keine Daten eingegeben.", "error")
            return redirect(url_for("signal_import"))
        imported, errors = _parse_signal_csv(raw, sections)
        if imported:
            existing = load_signals()
            existing.extend(imported)
            save_signals(existing)
        msg_parts = []
        if imported:
            msg_parts.append(f"{len(imported)} Signal{'e' if len(imported)!=1 else ''} importiert")
        if errors:
            msg_parts.append(f"{len(errors)} Zeile{'n' if len(errors)!=1 else ''} übersprungen")
        flash((" · ".join(msg_parts)) or "Nichts importiert.", "success" if imported else "error")
        return redirect(url_for("signal_list"))
    return render_template("admin_signals_import.html",
        categories=CATEGORIES, status_labels=STATUS_LABELS, sections=sections)


def _parse_signal_csv(raw, sections):
    """Parse CSV or TSV text into signal dicts. Returns (imported, errors)."""
    # Detect separator: tab wins if present on first line, then semicolon, then comma
    first_line = raw.split("\n")[0]
    if "\t" in first_line:
        sep = "\t"
    elif ";" in first_line:
        sep = ";"
    else:
        sep = ","

    # Build lookup maps
    status_map = {
        "radar": "radar", "im radar": "radar",
        "develop": "develop", "in entwicklung": "develop", "entwicklung": "develop",
        "announced": "announced", "angekündigt": "announced", "angekundigt": "announced",
        "active": "active", "verfügbar": "active", "in kraft": "active", "verfugbar": "active",
        "action": "action", "handlungsbedarf": "action",
    }
    cat_map = {
        "krankenkassen": "krankenkassen", "krankenkassen & gkv": "krankenkassen", "gkv": "krankenkassen",
        "digital": "digital", "digitalisierung": "digital", "digitalisierung & ti": "digital", "ti": "digital",
        "gesetze": "gesetze", "regulatorien": "gesetze", "gesetze & regulatorien": "gesetze",
        "personal": "personal", "tarife": "personal", "personal & tarife": "personal",
        "praxis": "praxis", "praxismanagement": "praxis",
    }
    sec_by_name = {s["name"].lower(): s["id"] for s in sections}
    sec_by_id   = {s["id"]: s["id"] for s in sections}

    # Header aliases → field name
    col_map = {
        "titel": "title", "title": "title",
        "zusammenfassung": "summary", "summary": "summary",
        "detail": "detail", "detailtext": "detail",
        "status": "status",
        "kategorie": "category", "category": "category",
        "fachbereich": "section_ids", "fachbereiche": "section_ids", "section_ids": "section_ids", "sektionen": "section_ids",
        "quelle": "source", "source": "source",
        "quelle-url": "source_url", "quellenurl": "source_url", "source_url": "source_url", "url": "source_url",
        "datum": "date", "date": "date",
        "frist": "deadline", "deadline": "deadline",
        "agent": "agent",
    }

    reader = csv.DictReader(io.StringIO(raw), delimiter=sep)
    # Normalise header names
    if reader.fieldnames is None:
        return [], ["Keine Spaltenüberschriften erkannt"]
    headers = {h: col_map.get(h.strip().lower().replace(" ", "-"), None) for h in reader.fieldnames}

    imported, errors = [], []
    for i, row in enumerate(reader, start=2):
        mapped = {}
        for raw_col, field in headers.items():
            if field and raw_col in row:
                mapped[field] = (row[raw_col] or "").strip()

        title = mapped.get("title", "")
        if not title:
            errors.append(f"Zeile {i}: kein Titel")
            continue

        # Resolve status
        raw_status = mapped.get("status", "radar").lower().strip()
        status = status_map.get(raw_status, "radar")

        # Resolve category
        raw_cat = mapped.get("category", "").lower().strip()
        category = cat_map.get(raw_cat, "praxis")

        # Resolve section_ids
        raw_secs = mapped.get("section_ids", "")
        sec_ids = []
        for part in raw_secs.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            sid = sec_by_id.get(part) or sec_by_name.get(part.lower())
            if sid:
                sec_ids.append(sid)

        sig = {
            "id":         next_signal_raw_id(),
            "agent":      mapped.get("agent", ""),
            "title":      title,
            "summary":    mapped.get("summary", ""),
            "detail":     mapped.get("detail", ""),
            "status":     status,
            "category":   category,
            "source":     mapped.get("source", ""),
            "source_url": mapped.get("source_url", ""),
            "date":       mapped.get("date", datetime.now().strftime("%Y-%m-%d")),
            "deadline":   mapped.get("deadline", ""),
            "section_ids": sec_ids,
            "created_at": datetime.now().isoformat(),
        }
        imported.append(sig)
    return imported, errors


# ── Helpers ───────────────────────────────────────────────────────────────────

def _create_user(form, redirect_to, force_entity=None, force_role=None):
    username = form.get("username", "").strip()
    password = form.get("password", "").strip()
    name     = form.get("name", "").strip()
    role     = force_role or form.get("role", "user")
    entity_id = force_entity or form.get("entity_id") or None

    if not username or not password:
        flash("Benutzername und Passwort sind erforderlich.", "error")
        return redirect(redirect_to)
    users = load_users()
    if any(u["username"] == username for u in users):
        flash(f"Benutzername '{username}' existiert bereits.", "error")
        return redirect(redirect_to)
    users.append({
        "id":        str(uuid.uuid4()),
        "username":  username,
        "pass_hash": _hash(password),
        "role":      role,
        "entity_id": entity_id,
        "name":      name or username,
    })
    save_users(users)
    flash(f"Nutzer '{username}' angelegt.", "success")
    return redirect(redirect_to)

def _signal_from_form(form, existing_id=None):
    sources = []
    for i in range(1, 4):
        name = form.get(f"source_{i}_name", "").strip()
        url  = form.get(f"source_{i}_url",  "").strip()
        date = form.get(f"source_{i}_date", "").strip()
        if name or url:
            sources.append({"name": name, "url": url, "date": date})
    primary_date = sources[0]["date"] if sources and sources[0].get("date") else datetime.now().strftime("%Y-%m-%d")
    return {
        "id":                existing_id or str(uuid.uuid4()),
        "title":             form.get("title", "").strip(),
        "summary":           form.get("summary", "").strip(),
        "detail":            form.get("detail", "").strip(),
        "category":          form.get("category", "gesetze"),
        "priority":          form.get("priority", "sollte"),
        "entwicklungsstand": form.get("entwicklungsstand", "beobachtung"),
        "handlungszeitpunkt":form.get("handlungszeitpunkt", ""),
        "naechster_schritt": form.get("naechster_schritt", "").strip(),
        "betroffene_rollen": form.getlist("betroffene_rollen"),
        "aufwand":           form.get("aufwand", ""),
        "themen_id":         form.get("themen_id", "").strip(),
        "sources":           sources,
        "date":              primary_date,
        "deadline":          form.get("deadline", "").strip() or None,
        "section_ids":       form.getlist("section_ids"),
        "reporting_status":  form.get("reporting_status", "").strip(),
        "agent":             form.get("agent", "").strip(),
    }


# ── Signal MATCH / FINAL ─────────────────────────────────────────────────────

@app.route("/admin/signals/match", methods=["GET", "POST"])
@role_required("admin", "superadmin")
def signal_match():
    matches = load_signal_matches()
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "add":
            row = {
                "id": str(uuid.uuid4()),
                "sig1": request.form.get("sig1", "").strip(),
                "pairs": []
            }
            for i in range(2, 6):
                sig_id = request.form.get(f"sig{i}", "").strip()
                sig_type = request.form.get(f"sig{i}_type", "").strip()
                if sig_id:
                    row["pairs"].append({"id": sig_id, "type": sig_type})
            matches.append(row)
            save_signal_matches(matches)
            flash("Gruppe gespeichert.", "success")
        elif action == "delete":
            row_id = request.form.get("row_id", "")
            matches = [m for m in matches if m["id"] != row_id]
            save_signal_matches(matches)
            flash("Gruppe gelöscht.", "success")
        return redirect(url_for("signal_match"))
    signals = load_signals()
    sig_map = {s["id"]: s.get("title", s["id"]) for s in signals}
    return render_template("admin_signal_matches.html", matches=matches, sig_map=sig_map)

@app.route("/admin/signals/final", methods=["GET", "POST"])
@role_required("admin", "superadmin")
def signal_final():
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "import_csv":
            raw = request.form.get("csv_data", "").strip()
            if raw:
                sections = load_sections()
                imported, errors = _parse_signal_csv(raw, sections)
                if imported:
                    archive_signal_final()
                    ts = datetime.now().strftime("%y%m%d%H%M")
                    save_signal_final(imported, ts)
                    flash(f"{len(imported)} Signale als neue FINAL-Version gespeichert (Timestamp: {ts}).", "success")
                if errors:
                    for e in errors[:5]:
                        flash(e, "error")
        elif action == "delete":
            sig_id = request.form.get("sig_id", "")
            data = load_signal_final()
            data["signals"] = [s for s in data["signals"] if s["id"] != sig_id]
            save_signal_final(data["signals"], data["timestamp"])
            flash("Signal gelöscht.", "success")
        return redirect(url_for("signal_final"))
    data = load_signal_final()
    return render_template("admin_signal_final.html",
        signals=data["signals"], timestamp=data["timestamp"],
        categories=CATEGORIES, priority_labels=SIGNAL_PRIORITY_LABELS,
        entwicklungsstand_labels=ENTWICKLUNGSSTAND_LABELS)

@app.route("/admin/signals/archive")
@role_required("admin", "superadmin")
def signal_archive():
    archives = list_signal_final_archives()
    return render_template("admin_signal_archive.html", archives=archives)

@app.route("/admin/signals/archive/<ts>")
@role_required("admin", "superadmin")
def signal_archive_view(ts):
    data = load_signal_final_archive(ts)
    if not data:
        abort(404)
    signals = data.get("signals", []) if isinstance(data, dict) else data
    timestamp = data.get("timestamp", ts) if isinstance(data, dict) else ts
    return render_template("admin_signal_archive_view.html",
        signals=signals, timestamp=timestamp, ts=ts,
        categories=CATEGORIES, priority_labels=SIGNAL_PRIORITY_LABELS)


# ── Signal Excel-Exports ─────────────────────────────────────────────────────

def _signals_to_xlsx(signals, filename):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    import io
    wb = Workbook()
    ws = wb.active
    ws.title = "Signale"
    headers = [
        "ID", "Titel", "Zusammenfassung", "Detail",
        "Kategorie", "Priorität", "Entwicklungsstand", "Reporting-Status",
        "Datum", "Frist",
        "Quelle 1", "Quellenlink 1", "Datum Q1",
        "Quelle 2", "Quellenlink 2", "Datum Q2",
        "Quelle 3", "Quellenlink 3", "Datum Q3",
    ]
    ws.append(headers)
    header_fill = PatternFill("solid", fgColor="1D3D28")
    header_font = Font(color="FFFFFF", bold=True, size=10)
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(wrap_text=False)
    prio_map = {"muss": "MUSS", "sollte": "SOLLTE", "kann": "KANN"}
    for s in signals:
        src = s.get("sources", [])
        def src_f(i, field): return src[i].get(field, "") if len(src) > i else ""
        ws.append([
            s.get("id", ""),
            s.get("title", ""),
            s.get("summary", ""),
            s.get("detail", ""),
            s.get("category", ""),
            prio_map.get(s.get("priority", ""), s.get("priority", "")),
            s.get("entwicklungsstand", ""),
            s.get("reporting_status", ""),
            s.get("date", ""),
            s.get("deadline", "") or "",
            src_f(0, "name"), src_f(0, "url"), src_f(0, "date"),
            src_f(1, "name"), src_f(1, "url"), src_f(1, "date"),
            src_f(2, "name"), src_f(2, "url"), src_f(2, "date"),
        ])
    col_widths = [14, 40, 60, 60, 14, 10, 18, 14, 12, 12, 20, 40, 12, 20, 40, 12, 20, 40, 12]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = w
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    from flask import send_file
    return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name=filename)

@app.route("/admin/signals/export/match")
@role_required("admin", "superadmin")
def signal_export_match():
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    import io
    matches = load_signal_matches()
    wb = Workbook()
    ws = wb.active
    ws.title = "MATCH"
    headers = ["sig1", "sig2", "sig2_type", "sig3", "sig3_type", "sig4", "sig4_type", "sig5", "sig5_type"]
    ws.append(headers)
    hfill = PatternFill("solid", fgColor="1D3D28")
    hfont = Font(color="FFFFFF", bold=True, size=10)
    for cell in ws[1]:
        cell.fill = hfill; cell.font = hfont
    for row in matches:
        r = [row.get("sig1", "")]
        for p in (row.get("pairs", []) + [{}, {}, {}, {}])[:4]:
            r += [p.get("id", ""), p.get("type", "")]
        ws.append(r)
    for i, w in enumerate([16, 16, 6, 16, 6, 16, 6, 16, 6], 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = w
    buf = io.BytesIO()
    wb.save(buf); buf.seek(0)
    from flask import send_file
    return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name=f"signale_match_{datetime.now().strftime('%Y%m%d')}.xlsx")

@app.route("/admin/signals/export/raw")
@role_required("admin", "superadmin")
def signal_export_raw():
    signals = load_signals()
    signals.sort(key=lambda s: s.get("id", ""))
    return _signals_to_xlsx(signals, f"signale_raw_{datetime.now().strftime('%Y%m%d')}.xlsx")

@app.route("/admin/signals/export/final")
@role_required("admin", "superadmin")
def signal_export_final():
    data = load_signal_final()
    signals = sorted(data["signals"], key=lambda s: s.get("id", ""))
    return _signals_to_xlsx(signals, f"signale_final_{datetime.now().strftime('%Y%m%d')}.xlsx")


# ── Agent Reports ─────────────────────────────────────────────────────────────

@app.route("/admin/agent-reports/raw", methods=["GET", "POST"])
@role_required("admin", "superadmin")
def agent_reports_raw():
    reports = load_agent_reports()
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "import":
            raw = request.form.get("tsv_data", "").strip()
            imported = 0
            for line in raw.splitlines():
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                source_name   = parts[0].strip()
                signal_count_s = parts[1].strip()
                if not source_name or source_name.lower() in ("quelle", "source"):
                    continue  # header row
                try:
                    signal_count = int(signal_count_s)
                except ValueError:
                    signal_count = 0
                issues    = parts[2].strip() if len(parts) > 2 else ""
                timestamp = parts[3].strip() if len(parts) > 3 else datetime.now().strftime("%y%m%d%H%M")
                agent_name = parts[4].strip() if len(parts) > 4 else ""
                reports.append({
                    "id": str(uuid.uuid4()),
                    "source_name": source_name,
                    "signal_count": signal_count,
                    "issues": issues,
                    "timestamp": timestamp,
                    "agent_name": agent_name,
                })
                imported += 1
            save_agent_reports(reports)
            flash(f"{imported} Einträge importiert.", "success")
        elif action == "delete_selected":
            ids_to_delete = set(request.form.getlist("delete_ids"))
            reports = [r for r in reports if r["id"] not in ids_to_delete]
            save_agent_reports(reports)
            flash(f"{len(ids_to_delete)} Einträge gelöscht.", "success")
        return redirect(url_for("agent_reports_raw"))
    reports_sorted = sorted(reports, key=lambda r: r.get("timestamp", ""), reverse=True)
    return render_template("admin_agent_reports_raw.html", reports=reports_sorted)

@app.route("/admin/agent-reports")
@role_required("admin", "superadmin")
def agent_reports():
    reports = load_agent_reports()
    date_from = request.args.get("date_from", "")
    date_to   = request.args.get("date_to", "")
    # Filter by timestamp (JJMMDD prefix comparison)
    def ts_to_date(ts):
        if len(ts) >= 6:
            try:
                return datetime.strptime("20" + ts[:6], "%Y%m%d").date()
            except ValueError:
                pass
        return None
    if date_from or date_to:
        try:
            df = datetime.strptime(date_from, "%Y-%m-%d").date() if date_from else None
        except ValueError:
            df = None
        try:
            dt = datetime.strptime(date_to, "%Y-%m-%d").date() if date_to else None
        except ValueError:
            dt = None
        filtered = []
        for r in reports:
            rd = ts_to_date(r.get("timestamp", ""))
            if rd is None:
                filtered.append(r)
                continue
            if df and rd < df:
                continue
            if dt and rd > dt:
                continue
            filtered.append(r)
        reports = filtered
    # Build stats per agent
    SEARCH_AGENTS = [("scrape", "Scrape-Agent"), ("feed", "Feed-Agent"),
                     ("pdf", "PDF-Agent"), ("search", "Search-Agent")]
    stats = {}
    for key, label in SEARCH_AGENTS:
        agent_rows = [r for r in reports if r.get("agent_name") == key]
        by_source = {}
        for r in agent_rows:
            sn = r["source_name"]
            if sn not in by_source:
                by_source[sn] = {"count": 0, "issues": [], "timestamps": []}
            by_source[sn]["count"] += r.get("signal_count", 0)
            if r.get("issues"):
                by_source[sn]["issues"].append(r["issues"])
            by_source[sn]["timestamps"].append(r.get("timestamp", ""))
        high, mid, low, zero, error = [], [], [], [], []
        for sn, data in by_source.items():
            entry = {"source": sn, "count": data["count"],
                     "issues": "; ".join(set(data["issues"])),
                     "timestamps": data["timestamps"]}
            if data["issues"]:
                error.append(entry)
            elif data["count"] >= 5:
                high.append(entry)
            elif data["count"] >= 2:
                mid.append(entry)
            elif data["count"] == 1:
                low.append(entry)
            else:
                zero.append(entry)
        stats[key] = {
            "label": label,
            "high": sorted(high, key=lambda x: -x["count"]),
            "mid":  sorted(mid,  key=lambda x: -x["count"]),
            "low":  low,
            "zero": zero,
            "error": error,
        }
    return render_template("admin_agent_reports.html",
        stats=stats, date_from=date_from, date_to=date_to,
        search_agents=SEARCH_AGENTS)


# ── Agents ────────────────────────────────────────────────────────────────────

@app.route("/admin/agents", methods=["GET"])
@role_required("admin", "superadmin")
def agents():
    cfg          = load_agent_configs()
    sources_list = load_sources()
    settings_cfg = load_settings()
    hints        = settings_cfg.get("source_prompt_hints", "")
    src_counts   = {}
    for key, *_ in [("scrape",), ("feed",), ("pdf",), ("search",)]:
        src_counts[key] = sum(1 for s in sources_list if key in s.get("ingestion_methods", []))
    return render_template("agents.html",
        agent_configs=cfg, sources=sources_list, hints=hints,
        src_counts=src_counts)

@app.route("/admin/agents/save", methods=["POST"])
@role_required("superadmin")
def agents_save():
    cfg = load_agent_configs()
    field = request.form.get("field", "")
    agent_key = request.form.get("agent_key", "")
    value = request.form.get("value", "").strip()
    if field == "output_format":
        cfg["output_format"] = value
    elif field in ("persona", "method", "hint") and agent_key in cfg["agents"]:
        cfg["agents"][agent_key][field] = value
    elif field in ("date_from", "date_to", "last_date_from", "last_date_to") and agent_key in cfg["agents"]:
        cfg["agents"][agent_key][field] = value
    save_agent_configs(cfg)
    return ("", 204)


# ── Sources ───────────────────────────────────────────────────────────────────

SOURCE_PRIMARY_CATEGORIES = [
    "Abrechnung & Honorar",
    "Arzneimittel & Sicherheit",
    "Berufsrecht & Standespolitik",
    "DMP & Chronikerversorgung",
    "Diagnostik & Leitlinien",
    "Digitalisierung & TI",
    "EU-Regulatorik",
    "Evidenz & Nutzenbewertung",
    "Fachliches & Leitlinien",
    "Fachmedien & Trends",
    "GKV, Erstattung & Verträge",
    "Gesetzgebung & Politik",
    "Hygiene & Arbeitsschutz",
    "IT-Sicherheit & Datenschutz",
    "Impfungen & Prävention",
    "Infektionsschutz & Public Health",
    "International & Trends",
    "Markt & Produkte",
    "Medizinprodukte",
    "PKV, Gebühren & Erstattung",
    "Praxisbetrieb & Selbstverwaltung",
    "Praxismanagement",
    "Public Health & Prävention",
    "Qualität & Patientensicherheit",
    "Regionalrecht & Vollzug",
    "Regulatorik & Richtlinien",
    "Röntgen & Strahlenschutz",
    "Verordnung & Arzneimittel",
    "Versorgungsforschung & Daten",
    "Wissenschaft & Studien",
    "Sonstiges",
]
SOURCE_SECONDARY_CATEGORIES = [
    "Abrechnung", "Arzneimittel", "Bekanntmachungen", "Berichte", "Berufsrecht",
    "Berufe", "Chroniker", "Datenschutz", "Diagnostik", "Digitalisierung",
    "DMP", "EU-Recht", "Fachpresse", "GKV", "Gesetz", "Gesetzgebungsverfahren",
    "GOÄ", "GOZ", "HzV", "Hygiene", "Impfungen", "IT-Sicherheit",
    "KV-Recht", "Leitlinien", "Markt", "Medizinprodukte", "Nutzenbewertung",
    "PKV", "Prävention", "Praxismanagement", "Public Health", "Qualität",
    "Röntgen", "Selbstverwaltung", "Sozialrecht", "Stellungnahmen",
    "Strategien", "TI", "Tarifrecht", "Vergütung", "Verordnungen",
    "Versorgung", "Wissenschaft", "Zulassung",
]
SOURCE_RELEVANT_ROLES = [
    "Abrechnung", "IT", "MFA", "MFA/ZFA", "Praxisinhaber",
    "Praxismanagement", "QM/Hygiene", "ZFA", "ZMV/Abrechnung",
    "Zahnärztlicher Dienst", "Ärztlicher Dienst", "Ärztlicher/Zahnärztlicher Dienst",
]
SOURCE_ZUGANG_OPTIONS = [
    "öffentlich",
    "öffentlich/teilweise Login",
    "öffentlich/teilweise Mitgliederportal",
    "öffentlich/teilweise Paywall",
    "öffentlich/teilweise Volltext",
    "öffentlich/teilweise Lizenz",
    "öffentlich/Interaktion Login",
    "teilweise Paywall",
    "kommerziell",
    "Login/kommerziell",
]
SOURCE_UPDATE_OPTIONS = [
    "laufend", "täglich", "täglich/wöchentlich", "wöchentlich",
    "laufend/änderungsbezogen", "monatlich/änderungsbezogen", "monatlich",
    "quartalsweise/laufend", "quartalsweise", "änderungsbezogen",
    "ereignisbezogen", "jährlich",
]
SOURCE_INGESTION_METHODS = [
    ("scrape",  "Scrape",       "HTML-Seiten automatisiert abrufen und auslesen"),
    ("feed",    "Feed / API",   "RSS-, Atom- oder API-Endpunkte abonnieren"),
    ("pdf",     "PDF",          "Dokumente herunterladen und extrahieren"),
    ("search",  "Suche / Login","Suche, Login-Bereich oder Partnerschaftszugang"),
]
SOURCE_PRIORITIES = ["hoch", "mittel", "niedrig"]
SOURCE_STATUSES   = ["geplant", "in_recherche", "aktiv", "inaktiv"]
SOURCE_STATUS_LABELS = {
    "geplant":       "Geplant",
    "in_recherche":  "In Recherche",
    "aktiv":         "Aktiv",
    "inaktiv":       "Inaktiv",
}

@app.route("/admin/sources", methods=["GET", "POST"])
@role_required("admin", "superadmin")
def sources():
    cfg = load_settings()
    if request.method == "POST":
        action = request.form.get("action", "")
        sources_list = load_sources()

        if action == "new":
            src = {
                "id":                   str(uuid.uuid4()),
                "kuerzel":              request.form.get("kuerzel", "").strip(),
                "name":                 request.form.get("name", "").strip(),
                "url":                  request.form.get("url", "").strip(),
                "primary_category":     request.form.get("primary_category", "Sonstiges"),
                "secondary_categories": request.form.getlist("secondary_categories"),
                "relevant_roles":       request.form.getlist("relevant_roles"),
                "ingestion_methods":    request.form.getlist("ingestion_methods"),
                "priority":             request.form.get("priority", "mittel"),
                "status":               request.form.get("status", "").strip(),
                "zugang":               request.form.get("zugang", "").strip(),
                "expected_update":      request.form.get("expected_update", "").strip(),
                "notes":                request.form.get("notes", "").strip(),
                "created_at":           datetime.now().strftime("%Y-%m-%d"),
            }
            sources_list.append(src)
            save_sources(sources_list)
            flash("Quelle angelegt.", "success")

        elif action == "edit":
            sid = request.form.get("source_id", "")
            for s in sources_list:
                if s["id"] == sid:
                    s["kuerzel"]              = request.form.get("kuerzel", "").strip()
                    s["name"]                 = request.form.get("name", "").strip()
                    s["url"]                  = request.form.get("url", "").strip()
                    s["primary_category"]     = request.form.get("primary_category", "Sonstiges")
                    s["secondary_categories"] = request.form.getlist("secondary_categories")
                    s["relevant_roles"]       = request.form.getlist("relevant_roles")
                    s["ingestion_methods"]    = request.form.getlist("ingestion_methods")
                    s["priority"]             = request.form.get("priority", "mittel")
                    s["status"]               = request.form.get("status", "").strip()
                    s["zugang"]               = request.form.get("zugang", "").strip()
                    s["expected_update"]      = request.form.get("expected_update", "").strip()
                    s["notes"]                = request.form.get("notes", "").strip()
                    agent_hints = []
                    for i in ("1", "2"):
                        agent = request.form.get(f"agent_hint_{i}_agent", "").strip()
                        text  = request.form.get(f"agent_hint_{i}_text", "").strip()
                        if agent or text:
                            agent_hints.append({"agent": agent, "text": text})
                    s["agent_hints"] = agent_hints
                    break
            save_sources(sources_list)
            flash("Quelle aktualisiert.", "success")

        elif action == "delete":
            sid = request.form.get("source_id", "")
            save_sources([s for s in sources_list if s["id"] != sid])
            flash("Quelle gelöscht.", "success")

        elif action == "hints":
            cfg["source_prompt_hints"]      = request.form.get("source_prompt_hints", "").strip()
            cfg["source_output_format"]     = request.form.get("source_output_format", "").strip()
            save_settings(cfg)
            flash("Hinweise gespeichert.", "success")

        return redirect(url_for("sources"))

    default_output_fmt = (
        "**[KÜRZEL] Quelle:** Name der Quelle\n"
        "**Datum:** TT.MM.JJJJ (soweit bekannt)\n"
        "**Titel:** Kurzer beschreibender Titel\n"
        "**Zusammenfassung:** 2–4 Sätze, die den Inhalt und die Relevanz für Gesundheitseinrichtungen erklären\n"
        "**Betroffene Rollen:** (z.B. Praxisinhaber, Abrechnung, QM, Datenschutz)\n"
        "**Priorität:** Hoch / Mittel / Niedrig\n"
        "**Link:** Direkter Link zur Quelle\n\n---"
    )
    sources_list = load_sources()
    hints = cfg.get("source_prompt_hints", "")
    source_output_format = cfg.get("source_output_format", default_output_fmt)
    return render_template("sources.html",
        sources=sources_list, hints=hints,
        source_output_format=source_output_format,
        source_primary_categories=SOURCE_PRIMARY_CATEGORIES,
        source_secondary_categories=SOURCE_SECONDARY_CATEGORIES,
        source_relevant_roles=SOURCE_RELEVANT_ROLES,
        source_ingestion_methods=SOURCE_INGESTION_METHODS,
        source_zugang_options=SOURCE_ZUGANG_OPTIONS,
        source_update_options=SOURCE_UPDATE_OPTIONS,
        source_priorities=SOURCE_PRIORITIES,
        source_statuses=SOURCE_STATUSES,
        source_status_labels=SOURCE_STATUS_LABELS)

@app.route("/admin/sources/import", methods=["POST"])
@role_required("admin", "superadmin")
def sources_import():
    rows = request.get_json(silent=True) or []
    if not rows:
        return jsonify({"ok": False, "msg": "Keine Daten"}), 400
    sources_list = load_sources()
    added = 0
    for row in rows[:20]:
        name = (row.get("name") or "").strip()
        if not name:
            continue
        sources_list.append({
            "id":                   str(uuid.uuid4()),
            "kuerzel":              (row.get("kuerzel") or "").strip(),
            "name":                 name,
            "url":                  (row.get("url") or "").strip(),
            "primary_category":     row.get("primary_category") or row.get("category") or "Sonstiges",
            "secondary_categories": row.get("secondary_categories") if isinstance(row.get("secondary_categories"), list) else [],
            "relevant_roles":       row.get("relevant_roles") if isinstance(row.get("relevant_roles"), list) else [],
            "priority":             row.get("priority") or "mittel",
            "status":               (row.get("status") or "").strip(),
            "zugang":               (row.get("zugang") or "").strip(),
            "expected_update":      (row.get("expected_update") or "").strip(),
            "notes":      (row.get("notes") or "").strip(),
            "created_at": datetime.now().strftime("%Y-%m-%d"),
        })
        added += 1
    save_sources(sources_list)
    return jsonify({"ok": True, "added": added})


# ── 403 handler ───────────────────────────────────────────────────────────────

@app.errorhandler(403)
def forbidden(e):
    return render_template("403.html"), 403


# ── Deploy webhook ────────────────────────────────────────────────────────────

@app.route("/deploy", methods=["POST"])
def deploy():
    import signal as _signal
    token = request.args.get("token", "")
    if not DEPLOY_TOKEN or not hmac.compare_digest(token, DEPLOY_TOKEN):
        abort(403)
    repo = os.path.dirname(os.path.abspath(__file__))
    try:
        subprocess.run(["git", "pull", "--ff-only"], cwd=repo, check=True, capture_output=True)
        # Send SIGHUP to gunicorn master → graceful reload of all workers
        os.kill(os.getppid(), _signal.SIGHUP)
        return jsonify({"ok": True, "msg": "Pulled and reloading"})
    except subprocess.CalledProcessError as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5001)
