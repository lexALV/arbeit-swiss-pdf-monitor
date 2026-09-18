# arbeit.swiss-PDF-Monitor

Der Monitor überwacht auf den Publikationsseiten von arbeit.swiss ausschließlich
die Bereiche **Weisungen / AVIG-Praxis / Richtlinien** und **Audit Letter** in
Deutsch, Französisch und Italienisch.

Bei jedem Lauf werden die drei HTML-Seiten abgerufen. Titel, Seitendatum,
Downloadname und PDF-Adresse werden direkt aus dem serverseitig gelieferten HTML
gelesen. Anschließend wird jede verlinkte PDF vollständig gelesen und ein
SHA-256-Fingerabdruck berechnet. Die PDFs werden nicht dauerhaft gespeichert.

Damit werden erkannt:

- neue und entfernte Dokumente;
- geänderte Titel und Datumsangaben;
- neue PDF-Adressen bei unverändertem Seitendatum;
- still ersetzte PDFs unter derselben Adresse.

## Ausgabe

Der Lauf erzeugt unter `site`:

- `index.html`: statische Tabelle für WebSite-Watcher;
- `arbeit-swiss-pdfs.csv`: dieselben Werte als CSV;
- `manifest.json`: maschinenlesbare Fassung.

Die Dateien enthalten keinen Abrufzeitpunkt. Bei unveränderten Quellseiten und
PDFs bleibt die Ausgabe byte-identisch. Sortiert wird nach Sprache, Bereich,
Dokumenttitel und PDF-Adresse.

## Änderungen und Downloadpaket

Unter `state` liegt nur das kleine Manifest des letzten erfolgreich
veröffentlichten Stands, nicht die früheren PDFs. Der Monitor vergleicht damit
bei jedem Lauf Dokumentidentität, Titel, Datumsangaben, URL, Dateigröße und
SHA-256-Fingerabdruck.

Neue, geänderte und entfernte Dokumente erscheinen oben auf der Seite unter
**Letzte Änderungen**. Die aktuellen Fassungen neuer und geänderter PDFs werden
zusätzlich in `site/letzte-aenderungen.zip` zusammengefasst. Das Paket bleibt
bis zur nächsten festgestellten Änderung verfügbar. Alte PDF-Versionen werden
nicht archiviert; diese Aufgabe bleibt beim bestehenden Skript
`01_Weisungen_Update`.

Der Ordner `state-next` enthält den Zustand des gerade abgeschlossenen Laufs.
In GitHub wird er erst nach einer erfolgreichen Pages-Veröffentlichung nach
`state` übernommen. Schlägt die Veröffentlichung fehl, bleibt damit die vorige
Vergleichsgrundlage erhalten.

## Lokaler Lauf

Python 3.11 oder neuer genügt; externe Pakete werden nicht benötigt.

```powershell
python -m unittest discover -s tests -v
python .\monitor.py
```

Für einen Probelauf in ein anderes Verzeichnis:

```powershell
python .\monitor.py --output-dir "$env:TEMP\arbeit-swiss-monitor"
```

## GitHub Pages

Die vorbereitete GitHub Action kann täglich laufen und die statische Seite über
GitHub Pages veröffentlichen. Vor der ersten Veröffentlichung sind ein eigenes
Repository und die einmalige Auswahl **Settings > Pages > Source: GitHub
Actions** erforderlich.

Der Workflow ist außerdem manuell startbar. Bei einem Abruffehler endet der
Erzeugungsschritt vor dem Deployment; die bisher veröffentlichte Seite bleibt
dadurch bestehen.
