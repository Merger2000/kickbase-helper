# Drittanbieter

Die folgenden mitgelieferten Dateien haben eigene Lizenzbedingungen. Diese Hinweise
vergeben keine zusätzliche Lizenz für den übrigen Projektcode oder für abgerufene Daten.

| Bestandteil | Herkunft / Stand | Lizenztext |
|---|---|---|
| `app/web/static/vendor/chart.umd.min.js` | [Chart.js 4.5.1](https://github.com/chartjs/Chart.js/tree/v4.5.1) | [MIT](licenses/chartjs-MIT.txt) |
| `app/web/static/vendor/htmx.min.js` | [htmx 2.0.11](https://github.com/bigskysoftware/htmx/tree/v2.0.11) | [0BSD](licenses/htmx-0BSD.txt) |
| `app/web/static/fonts/archivo-*.woff2` | Archivo | [SIL Open Font License 1.1](app/web/static/fonts/OFL.txt) |
| `docs/kickbase-openapi.yaml` | [kevinskyba/kickbase-api-doc](https://github.com/kevinskyba/kickbase-api-doc/tree/9a371a708af11155e7d36c3a1a467c9d759e1fdc), API-Spec 4.5.0 | [MIT](licenses/kickbase-api-doc-MIT.txt) |

Die API-Spezifikation wurde mit der Originaldatei abgeglichen. In der mitgelieferten
Fassung wurden die eingebetteten Token-Beispiele durch Platzhalter ersetzt.
Die Spezifikation ist inoffiziell und verleiht keine Zugriffsrechte auf den Dienst.

Python-Abhängigkeiten werden über `uv.lock` bezogen; ihre jeweiligen Lizenzdateien
liegen in den installierten Paketen. Datenquellen und deren Verwendung stehen im
[README](README.md#datenquellen-und-spielregeln).

Die HTML-Testdaten unter `tests/fixtures/ligainsider/markup_*.html` enthalten nur für
Parser-Regressionsfälle benötigte Strukturen, synthetische Texte und Beispiel-URLs.
Werbung, Seiten-Skripte, Kommentare und die vollständigen ursprünglichen Seiten wurden
entfernt. Diese Testdaten sind kein Datenexport der Quelle.
