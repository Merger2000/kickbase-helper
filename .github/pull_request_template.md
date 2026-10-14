## Was

<!-- Kurz: was ändert sich für den Nutzer bzw. im Code? -->

## Warum

<!-- Auftrag/Checkbox aus HANDOFF.md, Fehlerbild oder Ziel -->

## Wie getestet

<!-- Befehle, Fixtures, PostgreSQL, Browser bei 375 px … -->

## Screenshots

<!-- Bei UI-Änderungen: Hell/Dunkel bei 375 px. Sonst Abschnitt löschen. -->

## Checkliste

- [ ] `uv run ruff check . && uv run ruff format --check . && uv run pytest -q -n auto` lokal grün
- [ ] Keine Secrets, Tokens oder Namen anderer Manager in Code, Fixtures oder diesem Text
- [ ] Neue Migration statt Änderung an einer deployten Revision (falls DB betroffen)
- [ ] `HANDOFF.md` aktualisiert
