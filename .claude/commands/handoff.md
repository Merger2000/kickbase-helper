---
description: Übergabe an den nächsten Agenten vorbereiten
---

Aktualisiere HANDOFF.md (Checkboxen, aktueller Stand mit Dateien, Entscheidungen mit Begründung,
offene Probleme) so, dass ein anderes Modell ohne Chatverlauf weitermachen kann. Kurz halten:
erledigte Blöcke nur einzeilig unter „Erledigt (Kurzfassung)“, unter „Aktueller Stand“ höchstens
die letzten 5 Einträge. Setze „Aktiver Agent“ auf „keiner“. Tests laufen lassen.

Committen **nie auf `main`**: Ist ein Feature-Branch offen, dort mit „wip: handoff“ committen und
pushen (der Stand landet mit dem PR auf `main`). Ist keiner offen, einen Branch
`chore/handoff-<datum>` anlegen, committen, PR öffnen, auf die CI warten und erst
bei Grün mergen (`sleep 20 && gh pr checks --watch --interval 30 --fail-fast && gh pr merge --squash --delete-branch`;
kein `--auto`, siehe AGENTS.md).
