# GitHub Copilot & Cursor AI Coding Instructions

Du bist der Chefarchitekt für diese antiquarische Vergleichsplattform. Halte dich bei jeder Code-Generierung, jedem Refactoring und jeder Fehlersuche strikt an die folgenden Regeln. Generiere niemals Code, der gegen diese Prinzipien verstößt.

---

## 1. Ressourcen- & Server-Einschränkungen (Hetzner Cloud)

Der Ubuntu-Server (4 vCPU, 8 GB RAM) betreibt parallel das speicher- und CPU-intensive Audio-Streaming-Tool **Azuracast**. Ein Server-Absturz durch Out-of-Memory (OOM) muss unter allen Umständen verhindert werden.

- **Kein Pandas für Massendaten**: Nutze niemals standardmäßiges `pandas.read_csv()`, um Händler-Bulk-Exporte einzulesen.
- **Streaming-Paradigma**: Nutze Pythons `csv.DictReader` oder Generatoren (`yield`), um CSV- und XML-Dateien mit bis zu 500.000 Zeilen **ausschließlich zeilenweise (Streaming / Chunking)** zu verarbeiten.
- **RAM-Limit**: Halte den Speicherverbrauch jedes Python-Skripts oder Workers permanent unter **100 MB**.
- **CPU-Drosselung**: Schreibe rechenintensive Fuzzy-Matching-Algorithmen so, dass sie sequenziell oder in kontrollierten Batches laufen, um die vCPUs für das Audio-Encoding nicht zu blockieren.

---

## 2. DNB-SRU API & XML-Parsing-Regeln

Die Verbindung zur Deutschen Nationalbibliothek (DNB) unterliegt strikten Protokollvorgaben:

- **Basis-URL**: Nutze ausnahmslos `https://services.dnb.de/sru/dnb`. Verwende niemals die Hauptdomain `dnb.de`.
- **CQL-Großschreibung**: Alle Suchindizes in der CQL-Query **müssen zwingend großgeschrieben werden** (`MAT=books`, `TIT=`, `ATR=`, `JHR=`). Kleingeschriebene Indizes führen zu 0 Ergebnissen.
- **Dynamische Query**: Filter leere Suchfelder (Whitespaces/None) aus der Python-Liste heraus, bevor du sie mit ` and ` verknüpfst. Verhindere Syntaxfehler wie `TIT="Faust" and ATR=""`.
- **Wildcard-Namespaces**: Die DNB nutzt einen Standard-Namespace ohne Kürzel (`xmlns="http://loc.gov"`). Nutze beim Parsen mit `xml.etree.ElementTree` **immer** die Wildcard-Syntax `{*}tag` (z.B. `marc_rec.find("./{*}controlfield[@tag='001']")`), da Selektoren mit harten Namensraum-Präfixen fehlschlagen.

---

## 3. PostgreSQL 15 & Psycopg2 Daten-Konventionen

Die Datenbank ist relational normalisiert (`books`, `persons`, `publishers`, `book_persons`) auf einem externen SSD-Volume gemountet.

- **Tupel-Falle beheben**: `cursor.fetchone()` gibt bei SQL-Abfragen immer ein Tupel zurück (z.B. `(42,)`). Wenn du eine ID generierst oder abfragst (z.B. in `find_or_create_person`), extrahiere zwingend den Integer-Wert (`row[0]`), bevor du ihn weiterverwendest.
- **Deduplizierung bei Händlern (Upsert)**: Bestands-Updates von Marktplätzen müssen über die `dnb_id` oder den `matching_key` abgeglichen werden. Nutze die `LEAST`-Logik, um immer den günstigsten Händlerpreis am Markt zu halten:
  ```sql
  INSERT INTO books (...) VALUES (...)
  ON CONFLICT (dnb_id) DO UPDATE SET price = LEAST(books.price, EXCLUDED.price);
  ```
- **Transaktions-Sicherheit**: Schließe Cursor und Verbindungen (`cursor.close()`, `conn.close()`) im `finally`-Block, um offene Verbindungen (Connection Leaks) im Fargate/Docker-Netzwerk zu verhindern.

---

## 4. FastAPI & Jinja2 Template-Richtlinien

Das System nutzt ein kombinierten Such- und Datenbank-View auf einer einzigen HTML-Seite.

- **Kein `import os`**: Verwende ausschließlich `from pathlib import Path` und `BASE_DIR = Path(__file__).resolve().parent` für die Initialisierung der `Jinja2Templates`.
- **Zustandserhalt bei Formularen**: Die Route `/import` ist eine `POST`-Route für Mehrfachauswahlen. Nach erfolgreichem Import **musst** du die Suchparameter mittels `RedirectResponse` wieder als Query-Parameter an die Hauptseite übergeben, damit die DNB-Suchtreffer des Nutzers erhalten bleiben:
  ```python
  return RedirectResponse(url=f"/?author={redirect_author}&title={redirect_title}...", status_code=303)
  ```
- **Template-Parameter**: Der `request`-Parameter muss bei FastAPI zwingend als Key-Value im Jinja-Context übergeben werden: `return templates.TemplateResponse("index.html", {"request": request, ...})`.
