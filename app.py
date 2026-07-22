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

DATA_DIR   = os.path.join(os.path.dirname(__file__), "data")
SIGNALS_FILE  = os.path.join(DATA_DIR, "signals.json")
USERS_FILE    = os.path.join(DATA_DIR, "users.json")
ENTITIES_FILE = os.path.join(DATA_DIR, "entities.json")

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
def dashboard():
    signals = load_signals()
    def sort_key(s):
        try:   si = STATUS_ORDER.index(s.get("status", "radar"))
        except ValueError: si = 99
        return (si, s.get("date", ""))
    signals.sort(key=sort_key)
    counts = {s: 0 for s in STATUS_ORDER}
    for sig in signals:
        if sig.get("status") in counts:
            counts[sig["status"]] += 1
    return render_template(
        "dashboard.html",
        signals=signals, counts=counts,
        categories=CATEGORIES, status_labels=STATUS_LABELS,
        now=datetime.now().strftime("%d. %B %Y, %H:%M"),
        total=len(signals),
    )


# ── Superadmin: Entity management ─────────────────────────────────────────────

@app.route("/superadmin")
@role_required("superadmin")
def sa_dashboard():
    entities = load_entities()
    users = load_users()
    # Count users per entity
    counts = {}
    for u in users:
        eid = u.get("entity_id")
        if eid:
            counts[eid] = counts.get(eid, 0) + 1
    return render_template("sa_dashboard.html",
        entities=entities, user_counts=counts, users=users,
        role_labels=ROLE_LABELS)

@app.route("/superadmin/entities/new", methods=["GET", "POST"])
@role_required("superadmin")
def sa_entity_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Name ist erforderlich.", "error")
            return render_template("sa_entity_form.html", entity=None)
        entities = load_entities()
        new_entity = {
            "id": str(uuid.uuid4()),
            "name": name,
            "created_at": datetime.now().strftime("%Y-%m-%d"),
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
    return render_template("sa_entity_form.html", entity=None)

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
    return render_template("admin_signals.html",
        signals=signals, categories=CATEGORIES, status_labels=STATUS_LABELS)

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
        action=url_for("signal_new"))

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
        action=url_for("signal_edit", sig_id=sig_id))

@app.route("/admin/signals/<sig_id>/delete", methods=["POST"])
@role_required("admin", "superadmin")
def signal_delete(sig_id):
    signals = [s for s in load_signals() if s["id"] != sig_id]
    save_signals(signals)
    flash("Signal gelöscht.", "success")
    return redirect(url_for("signal_list"))


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
        "id":       existing_id or str(uuid.uuid4()),
        "title":    form.get("title", "").strip(),
        "summary":  form.get("summary", "").strip(),
        "detail":   form.get("detail", "").strip(),
        "category": form.get("category", "gesetze"),
        "status":   form.get("status", "radar"),
        "source":   form.get("source", "").strip(),
        "source_url": form.get("source_url", "").strip(),
        "date":     form.get("date", datetime.now().strftime("%Y-%m-%d")),
        "deadline": form.get("deadline", "").strip() or None,
    }


# ── 403 handler ───────────────────────────────────────────────────────────────

@app.errorhandler(403)
def forbidden(e):
    return render_template("403.html"), 403


# ── Deploy webhook ────────────────────────────────────────────────────────────

@app.route("/deploy", methods=["POST"])
def deploy():
    token = request.args.get("token", "")
    if not DEPLOY_TOKEN or not hmac.compare_digest(token, DEPLOY_TOKEN):
        abort(403)
    repo = os.path.dirname(os.path.abspath(__file__))
    try:
        subprocess.run(["git", "pull", "--ff-only"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["systemctl", "restart", "branchenradar"], check=True, capture_output=True)
        return jsonify({"ok": True})
    except subprocess.CalledProcessError as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5001)
