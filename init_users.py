"""
Einmalig ausführen um die drei Test-Nutzer anzulegen.
  python init_users.py
"""
import json, hashlib, uuid, os

USERS_FILE = os.path.join(os.path.dirname(__file__), "data", "users.json")

def h(pw): return hashlib.sha256(pw.encode()).hexdigest()

users = [
    {
        "id": str(uuid.uuid4()),
        "username": "superadmin",
        "pass_hash": h("SuperAdmin2026!"),
        "role": "superadmin",
        "entity_id": None,
        "name": "Super Admin",
    },
    {
        "id": str(uuid.uuid4()),
        "username": "admin",
        "pass_hash": h("Admin2026!"),
        "role": "admin",
        "entity_id": "entity-demo-0001",
        "name": "Praxis Admin",
    },
    {
        "id": str(uuid.uuid4()),
        "username": "nutzer",
        "pass_hash": h("Nutzer2026!"),
        "role": "user",
        "entity_id": "entity-demo-0001",
        "name": "Max Mustermann",
    },
]

with open(USERS_FILE, "w", encoding="utf-8") as f:
    json.dump(users, f, ensure_ascii=False, indent=2)

print("✓ Nutzer angelegt:")
for u in users:
    print(f"  {u['role']:12}  username={u['username']}")
