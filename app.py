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

DATA_FILE = os.path.join(os.path.dirname(__file__), "data", "signals.json")
DEPLOY_TOKEN = os.environ.get("DEPLOY_TOKEN", "")

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS_HASH = os.environ.get("ADMIN_PASS_HASH", "")  # sha256 hex of password

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


def load_signals():
    if not os.path.exists(DATA_FILE):
        return []
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_signals(signals):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(signals, f, ensure_ascii=False, indent=2)


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


def check_password(password):
    if not ADMIN_PASS_HASH:
        return password == "admin"
    h = hashlib.sha256(password.encode()).hexdigest()
    return hmac.compare_digest(h, ADMIN_PASS_HASH)


# ── Public dashboard ──────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    signals = load_signals()
    # Sort: by status order, then by date descending
    def sort_key(s):
        try:
            si = STATUS_ORDER.index(s.get("status", "radar"))
        except ValueError:
            si = 99
        return (si, s.get("date", ""))
    signals.sort(key=sort_key)
    counts = {s: 0 for s in STATUS_ORDER}
    for sig in signals:
        st = sig.get("status", "radar")
        if st in counts:
            counts[st] += 1
    return render_template(
        "dashboard.html",
        signals=signals,
        counts=counts,
        categories=CATEGORIES,
        status_labels=STATUS_LABELS,
        now=datetime.now().strftime("%d. %B %Y, %H:%M"),
        total=len(signals),
    )


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = request.form.get("username", "")
        pw   = request.form.get("password", "")
        if user == ADMIN_USER and check_password(pw):
            session["logged_in"] = True
            session["username"] = user
            return redirect(request.args.get("next") or url_for("admin"))
        flash("Benutzername oder Passwort falsch.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("dashboard"))


# ── Admin ─────────────────────────────────────────────────────────────────────

@app.route("/admin")
@login_required
def admin():
    signals = load_signals()
    signals.sort(key=lambda s: s.get("date", ""), reverse=True)
    return render_template(
        "admin.html",
        signals=signals,
        categories=CATEGORIES,
        status_labels=STATUS_LABELS,
    )


@app.route("/admin/new", methods=["GET", "POST"])
@login_required
def signal_new():
    if request.method == "POST":
        sig = _signal_from_form(request.form)
        signals = load_signals()
        signals.append(sig)
        save_signals(signals)
        flash("Signal gespeichert.", "success")
        return redirect(url_for("admin"))
    return render_template(
        "signal_form.html",
        signal=None,
        categories=CATEGORIES,
        status_labels=STATUS_LABELS,
        action=url_for("signal_new"),
    )


@app.route("/admin/edit/<sig_id>", methods=["GET", "POST"])
@login_required
def signal_edit(sig_id):
    signals = load_signals()
    sig = next((s for s in signals if s["id"] == sig_id), None)
    if sig is None:
        abort(404)
    if request.method == "POST":
        updated = _signal_from_form(request.form, existing_id=sig_id)
        for i, s in enumerate(signals):
            if s["id"] == sig_id:
                signals[i] = updated
                break
        save_signals(signals)
        flash("Signal aktualisiert.", "success")
        return redirect(url_for("admin"))
    return render_template(
        "signal_form.html",
        signal=sig,
        categories=CATEGORIES,
        status_labels=STATUS_LABELS,
        action=url_for("signal_edit", sig_id=sig_id),
    )


@app.route("/admin/delete/<sig_id>", methods=["POST"])
@login_required
def signal_delete(sig_id):
    signals = [s for s in load_signals() if s["id"] != sig_id]
    save_signals(signals)
    flash("Signal gelöscht.", "success")
    return redirect(url_for("admin"))


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


# ── Deploy webhook ────────────────────────────────────────────────────────────

@app.route("/deploy", methods=["POST"])
def deploy():
    token = request.args.get("token", "")
    if not DEPLOY_TOKEN or not hmac.compare_digest(token, DEPLOY_TOKEN):
        abort(403)
    repo = os.path.dirname(os.path.abspath(__file__))
    try:
        subprocess.run(["git", "pull", "--ff-only"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["systemctl", "restart", "praxis-radar"], check=True, capture_output=True)
        return jsonify({"ok": True, "msg": "Deployed successfully"})
    except subprocess.CalledProcessError as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5001)
