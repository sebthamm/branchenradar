#!/bin/bash
# Wird beim Sitzungsstart aufgerufen. Meldet wenn der lokale Stand hinter GitHub liegt.

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

git fetch origin main --quiet 2>/dev/null || { echo "⚠ Git fetch fehlgeschlagen (kein Netz?). Stand unbekannt."; exit 0; }

BEHIND=$(git rev-list HEAD..origin/main --count 2>/dev/null)
AHEAD=$(git rev-list origin/main..HEAD --count 2>/dev/null)
DIRTY=$(git status --porcelain 2>/dev/null)

if [ "$BEHIND" -gt 0 ]; then
  echo "⚠ BRANCHENRADAR: Lokal ist $BEHIND Commit(s) hinter GitHub."
  echo "  Bitte zuerst: git pull"
  echo "  (Sonst geht Arbeit vom anderen Rechner verloren)"
elif [ "$AHEAD" -gt 0 ]; then
  echo "ℹ BRANCHENRADAR: $AHEAD lokaler Commit(s) noch nicht auf GitHub gepusht."
elif [ -n "$DIRTY" ]; then
  echo "ℹ BRANCHENRADAR: Lokale Änderungen vorhanden (noch nicht committet)."
else
  echo "✓ BRANCHENRADAR: Git-Stand aktuell."
fi
