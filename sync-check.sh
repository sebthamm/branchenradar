#!/bin/bash
# Wird beim Sitzungsstart aufgerufen. Meldet wenn der lokale Stand hinter GitHub liegt.

check_repo() {
  local LABEL="$1"
  local DIR="$2"

  if [ ! -d "$DIR/.git" ]; then
    echo "⚠ $LABEL: Kein Git-Repo unter $DIR"
    return
  fi

  cd "$DIR"
  git fetch origin main --quiet 2>/dev/null || { echo "⚠ $LABEL: Git fetch fehlgeschlagen (kein Netz?)."; return; }

  BEHIND=$(git rev-list HEAD..origin/main --count 2>/dev/null)
  AHEAD=$(git rev-list origin/main..HEAD --count 2>/dev/null)
  DIRTY=$(git status --porcelain 2>/dev/null)

  if [ "$BEHIND" -gt 0 ]; then
    echo "⚠ $LABEL: Lokal ist $BEHIND Commit(s) hinter GitHub — bitte zuerst: git pull"
  elif [ "$AHEAD" -gt 0 ]; then
    echo "ℹ $LABEL: $AHEAD Commit(s) noch nicht gepusht."
  elif [ -n "$DIRTY" ]; then
    echo "ℹ $LABEL: Lokale Änderungen vorhanden (noch nicht committet)."
  else
    echo "✓ $LABEL: aktuell."
  fi
}

check_repo "BRANCHENRADAR" "$HOME/praxis-radar"
check_repo "GEDÄCHTNIS    " "$HOME/.claude/projects/C--Users-thamm/memory"
