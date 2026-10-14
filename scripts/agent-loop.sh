#!/usr/bin/env bash
# Arbeitet die offenen Checkboxen aus HANDOFF.md („Plan“) nacheinander mit opencode ab.
# Jeder Schritt läuft in einer FRISCHEN opencode-Session (kleiner Kontext = wenig Tokens).
#
# Nutzung:
#   scripts/agent-loop.sh [MAX_SCHRITTE]        # Standard: 5
#   AGENT_MODEL=opencode-go/kimi-k2.7-code scripts/agent-loop.sh 3
#
# Stoppt, wenn
#   - MAX_SCHRITTE erreicht sind,
#   - ein Lauf keine Checkbox abgehakt hat (blockiert, Freigabe nötig, Fehler),
#     Ausnahme: Hat der Lauf Code geändert, aber nicht committet (z. B. Schritt-Limit erreicht),
#     startet einmal eine kurze Abschluss-Session (testen, abhaken, committen, pushen),
#   - die Datei .agent-stop existiert (zum Abbrechen von außen: `touch .agent-stop`).
# Logs liegen unter .agent-logs/ (gitignored).
set -euo pipefail

export PATH="$HOME/.opencode/bin:$PATH"
if ! command -v opencode >/dev/null 2>&1; then
  echo "Fehler: 'opencode' wurde nicht gefunden. Erwartet unter \$HOME/.opencode/bin/opencode," >&2
  echo "       ist aber auch nach export PATH=\"\$HOME/.opencode/bin:\$PATH\" nicht im PATH." >&2
  echo "       Bitte opencode installieren oder HOME anpassen und erneut starten." >&2
  exit 1
fi

cd "$(git rev-parse --show-toplevel)"

MAX_STEPS="${1:-5}"
MODEL="${AGENT_MODEL:-opencode-go/minimax-m3}"
LOG_DIR=".agent-logs"
mkdir -p "$LOG_DIR"

PROMPT='Arbeite genau EINEN Schritt ab: die erste offene Checkbox unter „Plan“ in HANDOFF.md,
nach dem Schritt-Ablauf in AGENTS.md. Arbeite zügig: sobald die Tests grün sind, sofort
HANDOFF.md abhaken, committen und pushen. Danach aufhören – keinen zweiten Schritt anfangen.
Braucht der Schritt eine Freigabe oder Entscheidung des Nutzers oder ist er blockiert: keinen
Code ändern, den Grund kurz unter „Offene Probleme“ in HANDOFF.md eintragen und aufhören.'

done_count() {
  git show HEAD:HANDOFF.md 2>/dev/null | grep -c '^- \[x\]' || true
}

FINISH_PROMPT='Der vorige Lauf hat die erste offene Checkbox unter „Plan“ in HANDOFF.md umgesetzt,
aber nicht abgeschlossen (Änderungen liegen uncommittet im Arbeitsverzeichnis). Nichts neu
bauen: git status/diff ansehen, die Tests aus AGENTS.md laufen lassen und nur Fehler beheben,
dann Checkbox abhaken, kurzen Eintrag unter „Aktueller Stand“, committen und pushen.
Ist der Schritt unfertig oder blockiert: Grund unter „Offene Probleme“ eintragen und aufhören.'

dirty() {
  [[ -n "$(git status --porcelain)" ]]
}

open_count() {
  sed -n '/^## Plan/,/^## Aktueller Stand/p' HANDOFF.md | grep -c '^- \[ \]' || true
}

rm -f .agent-stop
for ((i = 1; i <= MAX_STEPS; i++)); do
  if [[ -e .agent-stop ]]; then
    echo "Abbruch: .agent-stop gefunden."
    break
  fi
  if [[ "$(open_count)" -eq 0 ]]; then
    echo "Keine offenen Checkboxen mehr – fertig."
    break
  fi

  before="$(done_count)"
  log="$LOG_DIR/$(date +%Y%m%d-%H%M%S)-schritt-$i.log"
  echo "== Schritt $i/$MAX_STEPS mit $MODEL (Log: $log)"

  if ! opencode run --agent build --model "$MODEL" --auto --title "agent-loop $i" "$PROMPT" \
    >"$log" 2>&1; then
    echo "opencode ist mit Fehler beendet – siehe $log"
    break
  fi

  after="$(done_count)"
  if [[ "$after" -le "$before" ]] && dirty; then
    echo "   Änderungen nicht committet – Abschluss-Session (Log: ${log%.log}-abschluss.log)"
    if ! opencode run --agent build --model "$MODEL" --auto --title "agent-loop $i abschluss" \
      "$FINISH_PROMPT" >"${log%.log}-abschluss.log" 2>&1; then
      echo "opencode ist mit Fehler beendet – siehe ${log%.log}-abschluss.log"
      break
    fi
    after="$(done_count)"
  fi
  if [[ "$after" -le "$before" ]]; then
    echo "Kein Schritt abgehakt (blockiert oder Freigabe nötig) – siehe HANDOFF.md und $log"
    break
  fi
  echo "   erledigt ($before → $after abgehakt)"
done
