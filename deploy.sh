#!/bin/bash
# Deploy-Schutz: bricht ab wenn GitHub neuere Commits hat oder lokal ungepushte Änderungen existieren.

set -e

echo "=== Branchenradar Deploy ==="

# Git-Stand prüfen
git fetch origin main --quiet

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" != "$REMOTE" ]; then
  AHEAD=$(git rev-list origin/main..HEAD --count)
  BEHIND=$(git rev-list HEAD..origin/main --count)
  echo ""
  if [ "$BEHIND" -gt 0 ]; then
    echo "ABBRUCH: GitHub ist $BEHIND Commit(s) weiter als lokal."
    echo "Bitte zuerst: git pull"
    echo ""
    echo "Umgehung nur mit: DEPLOY_OHNE_PRUEFUNG=1 bash deploy.sh"
    exit 1
  fi
  if [ "$AHEAD" -gt 0 ]; then
    echo "ABBRUCH: Lokal sind $AHEAD Commit(s) noch nicht auf GitHub."
    echo "Bitte zuerst: git push"
    echo ""
    echo "Umgehung nur mit: DEPLOY_OHNE_PRUEFUNG=1 bash deploy.sh"
    exit 1
  fi
fi

# Ungepushte Änderungen prüfen
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "ABBRUCH: Es gibt lokale Änderungen, die noch nicht committet sind."
  echo "Bitte zuerst: git add -A && git commit && git push"
  echo ""
  echo "Umgehung nur mit: DEPLOY_OHNE_PRUEFUNG=1 bash deploy.sh"
  exit 1
fi

if [ "${DEPLOY_OHNE_PRUEFUNG}" = "1" ]; then
  echo "WARNUNG: Prüfung umgangen."
fi

# Deploy-Token vom Server holen und Webhook auslösen
echo ""
echo "Git-Stand OK. Starte Deploy..."

DEPLOY_TOKEN=$(ssh root@178.105.170.27 "grep DEPLOY_TOKEN /opt/branchenradar/.env | cut -d'=' -f2" 2>/dev/null)

if [ -z "$DEPLOY_TOKEN" ]; then
  echo "FEHLER: Deploy-Token konnte nicht vom Server gelesen werden."
  exit 1
fi

curl -s -X POST "http://178.105.170.27/deploy?token=${DEPLOY_TOKEN}"
echo ""
echo "Deploy abgeschlossen."
