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
    for name in ("signals", "users", "entities", "todos"):
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

def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        return {"default_todo_categories": DEFAULT_TODO_CATEGORIES}
    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_settings(d): _save(SETTINGS_FILE, d)

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
    eid = session.get("entity_id")
    return {
        "todo_categories": entity_todo_categories(eid),
        "pending_todos": sum(
            1 for t in load_todos()
            if not t.get("done") and (
                session.get("role") == "superadmin" or t.get("entity_id") == eid
            )
        ) if session.get("user_id") else 0,
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

    def sort_key(s):
        try:   si = STATUS_ORDER.index(s.get("status", "radar"))
        except ValueError: si = 99
        return (si, s.get("date", ""))
    signals.sort(key=sort_key)

    counts = {s: 0 for s in STATUS_ORDER}
    for sig in signals:
        if sig.get("status") in counts:
            counts[sig["status"]] += 1

    sec_counts = {}
    for sig in signals:
        for sid in sig.get("section_ids", []):
            sec_counts[sid] = sec_counts.get(sid, 0) + 1

    return render_template(
        "dashboard.html",
        signals=signals, counts=counts,
        categories=CATEGORIES, status_labels=STATUS_LABELS,
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
        default_cats = load_settings().get("default_todo_categories", DEFAULT_TODO_CATEGORIES)
        new_entity = {
            "id":               str(uuid.uuid4()),
            "name":             name,
            "created_at":       datetime.now().strftime("%Y-%m-%d"),
            "section_ids":      request.form.getlist("section_ids"),
            "todo_categories":  default_cats,
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

@app.route("/admin/signals")
@role_required("admin", "superadmin")
def signal_list():
    signals = load_signals()
    signals.sort(key=lambda s: s.get("date", ""), reverse=True)
    sections = load_sections()
    sec_map  = {s["id"]: s for s in sections}
    return render_template("admin_signals.html",
        signals=signals, categories=CATEGORIES, status_labels=STATUS_LABELS,
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
@role_required("admin", "superadmin")
def todos():
    eid      = session.get("entity_id")
    role     = session.get("role")
    all_todos = load_todos()
    if role == "superadmin":
        my_todos = all_todos
    else:
        my_todos = [t for t in all_todos if t.get("entity_id") == eid]
    sig_map = {s["id"]: s for s in load_signals()}
    cats    = entity_todo_categories(eid)
    return render_template("todos.html",
        todos=my_todos, sig_map=sig_map, categories=cats,
        status_labels=STATUS_LABELS)

@app.route("/todos/new", methods=["POST"])
@role_required("admin", "superadmin")
def todo_new():
    eid = session.get("entity_id")
    todo = {
        "id":         str(uuid.uuid4()),
        "signal_id":  request.form.get("signal_id", ""),
        "signal_title": request.form.get("signal_title", ""),
        "entity_id":  eid,
        "category":   request.form.get("category", ""),
        "deadline":   request.form.get("deadline", ""),
        "comment":    request.form.get("comment", "").strip(),
        "done":       False,
        "created_by": session.get("user_id"),
        "created_at": datetime.now().isoformat(),
    }
    todos = load_todos()
    todos.append(todo)
    save_todos(todos)
    flash("ToDo angelegt.", "success")
    next_url = request.form.get("next") or url_for("todos")
    return redirect(next_url)

@app.route("/todos/<todo_id>/toggle", methods=["POST"])
@role_required("admin", "superadmin")
def todo_toggle(todo_id):
    todos = load_todos()
    for t in todos:
        if t["id"] == todo_id:
            t["done"] = not t.get("done", False)
            if t["done"]:
                t["done_at"] = datetime.now().isoformat()
            else:
                t.pop("done_at", None)
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
        cats = [c.strip() for c in request.form.getlist("categories") if c.strip()]
        entity["todo_categories"] = cats
        save_entities(entities)
        flash("Einstellungen gespeichert.", "success")
        return redirect(url_for("settings"))
    cats = entity.get("todo_categories", DEFAULT_TODO_CATEGORIES)
    return render_template("settings.html", entity=entity, categories=cats)

@app.route("/superadmin/settings", methods=["GET", "POST"])
@role_required("superadmin")
def sa_settings():
    cfg = load_settings()
    if request.method == "POST":
        cats = [c.strip() for c in request.form.getlist("categories") if c.strip()]
        cfg["default_todo_categories"] = cats
        save_settings(cfg)
        flash("Standard-Kategorien gespeichert.", "success")
        return redirect(url_for("sa_settings"))
    cats = cfg.get("default_todo_categories", DEFAULT_TODO_CATEGORIES)
    return render_template("sa_settings.html", categories=cats)


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
            "id":         str(uuid.uuid4()),
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
    return {
        "id":          existing_id or str(uuid.uuid4()),
        "title":       form.get("title", "").strip(),
        "summary":     form.get("summary", "").strip(),
        "detail":      form.get("detail", "").strip(),
        "category":    form.get("category", "gesetze"),
        "status":      form.get("status", "radar"),
        "source":      form.get("source", "").strip(),
        "source_url":  form.get("source_url", "").strip(),
        "date":        form.get("date", datetime.now().strftime("%Y-%m-%d")),
        "deadline":    form.get("deadline", "").strip() or None,
        "section_ids": form.getlist("section_ids"),
    }


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
