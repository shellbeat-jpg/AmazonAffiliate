# ?? Antiquarian Book Comparison Platform & DNB Data Ingestion Pipeline

Dieses Projekt ist ein hochperformantes, speicherschonendes Vergleichsportal für bibliophile und antiquarische Bücher im Premium-Segment (50 € - 1000 €). Es kombiniert eine Live-Schnittstelle zur Deutschen Nationalbibliothek (DNB) mit einem relational normalisierten Persistenz-Speicher auf einer externen SSD.

---

## ?? System-Architektur & Hetzner-Infrastruktur

Das System läuft isoliert in einer **Docker Compose**-Umgebung auf einem Ubuntu-Server (Hetzner Cloud Cloud-Server mit dedizierten vCPUs, CCX-Linie), um Ressourcenkonflikte mit parallel laufenden Diensten (wie Azuracast/Liquidsoap Audio-Streaming) zu verhindern.

- **Frontend/Backend**: FastAPI (Python 3.10) auf Port `4000` (gesichert via Nginx Reverse Proxy und Let's Encrypt SSL).
- **Template-Engine**: Jinja2 für serverseitig gerendertes, schnelles HTML.
- **Datenbank**: PostgreSQL 15 (Alpine) auf einer externen Hetzner SSD (`Hetzner Volume`).
- **DB-Management**: pgAdmin 4 (isoliert im Docker-Netzwerk, erreichbar via Nginx-Subdomain).
- **Import Pipeline**: Asynchroner, zeilenweiser Python-Stream-Worker (Batching/Generatoren), um RAM-Spitzen unter 100 MB zu decken.

---

## ??? Datenbankschema (PostgreSQL 15)

Das Schema löst flache Strukturen in ein hochgradig relationales, normalisiertes Gefüge auf, um Daten-Duplikate bei Hunderttausenden Einträgen zu verhindern und schnelle Trigram- sowie Volltextsuchen zu ermöglichen.

### 1. `books` (Kerntabelle)
- `dnb_id` (TEXT, UNIQUE): Primärer Deduplizierungs-Schlüssel für Direkt-Importe aus dem Nationalkatalog.
- `matching_key` (TEXT, UNIQUE): Der berechnete bibliophile Fuzzy-Match-Key für plattformübergreifenden Bestandsabgleich (Format: `[autor]-[titel]-[jahr]-[seiten]`).
- `price` (NUMERIC(10,2)): Speichert den jeweils günstigsten am Markt ermittelten Händlerpreis (`LEAST`-Logik).
- `search_vector` (TSVECTOR): Generierte Spalte für integrierte PostgreSQL-Volltextsuche (Titel, Reihe, Beschreibung) mit deutschem Stemming.

### 2. `persons` & `book_persons` (N:M Assoziation)
- Autoren, Herausgeber, Illustratoren und Übersetzer werden in `persons` über die numerische `gnd_id` (Gemeinsame Normdatei) dedubliziert.
- Die Tabelle `book_persons` speichert die spezifische `role` und das Flag `is_primary_author` (entspricht MARC-Feld 100 vs. 700).

### 3. `publishers`
- Verlage werden als eigene Entität ausgelagert (Deduplizierung über Verlags-GND aus Feld 710 oder Unique Name).

---

## ?? DNB-SRU Ingestion & XML-Parsing-Logik

Die Datenbeschaffung erfolgt über die SRU-Schnittstelle der DNB unter Verwendung des **MARC21-xml** Schemas.

- **Endpunkt**: `https://services.dnb.de/sru/dnb`
- **Abfrage-Protokoll**: CQL (Contextual Query Language). Suchindizes (`MAT=books`, `TIT=`, `ATR=`, `JHR=`) **müssen zwingend großgeschrieben werden**, da die DNB Kleinschreibung mit leeren Trefferlisten quittiert.
- **Query-Generierung**: Vollständig dynamisch. Optionale oder leere Suchfelder werden vor der Transmission herausgefiltert, um fehlerhafte `and`-Verknüpfungen in der CQL-Syntax zu vermeiden.

### Namespace-unabhängiges Parsing (`xml.etree.ElementTree`)
Da die Nationalbibliothek MARC21-Dokumente oft mit einem Standard-Namespace ohne explizites XML-Präfix ausliefert (`<record xmlns="http://loc.gov">`), blockieren absolute Pfade mit festen Kürzeln. 
Der Parser nutzt daher die universelle **Wildcard-Syntax `{*}tag`** zur Extraktion der Felder:

- **DNB-ID**: `./{*}controlfield[@tag='001']`
- **Haupttitel**: `./{*}datafield[@tag='245']/{*}subfield[@code='a']`
- **Hauptautor**: `./{*}datafield[@tag='100']/{*}subfield[@code='a']`
- **Umfang / Seiten**: `./{*}datafield[@tag='300']/{*}subfield[@code='a']`
- **Verlag / Jahr**: Extrahierung aus `tag='264'` mit Fallback auf `tag='260'`.

---

## ?? Context-Richtlinien für GitHub Copilot / Cursor

Wenn du Code für dieses Projekt generierst oder erweiterst, beachte strikt folgende Architekturregeln:

1. **Kein `import os` für Pfade**: Nutze ausnahmslos `from pathlib import Path` und `BASE_DIR = Path(__file__).resolve().parent` für die Jinja2-Template-Verzeichnisse.
2. **Datenbank-Rückgaben beachten**: `cursor.fetchone()` liefert Tupel zurück (z.B. `(1,)`). Extrahiere Primärschlüssel beim `INSERT` immer über `row[0]`, bevor du sie an verknüpfte Tabellen übergibst, um Laufzeit-Abstürze zu verhindern.
3. **Zustandserhalt bei Redirects**: Nach einem POST-Sammelimport an `/import` muss die App mittels `RedirectResponse` zwingend die Suchparameter (`redirect_author`, `redirect_title` etc.) als Query-Parameter wieder in die Ziel-URL injizieren, damit die Suchergebnisse des Nutzers im Webinterface nicht verloren gehen.
4. **PostgreSQL-Deduplizierung**: Nutze beim Injezieren von Händlerdaten immer das Upsert-Verfahren:
   ```sql
   INSERT INTO books (...) VALUES (...)
   ON CONFLICT (dnb_id) DO UPDATE SET price = LEAST(books.price, EXCLUDED.price);
   ```
5. **Speicherschonend arbeiten**: Lade Massendaten (z.B. wöchentliche Marktplatz-CSVs mit 500.000 Zeilen) niemals komplett in den RAM (kein ungestreamtes Pandas). Nutze Generatoren (`yield`) und verarbeite Daten blockweise (Chunks), um die Azuracast-Streaming-Dienste auf dem Hetzner-Server nicht zu gefährden.
