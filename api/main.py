import xml.etree.ElementTree as ET
import re
import json
import base64
from pathlib import Path
from fastapi import FastAPI, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
import requests
import psycopg2

app = FastAPI()

# Pfad-Auflösung via Path (ohne os-Modul) - unabhängig vom Arbeitsverzeichnis
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR)

DB_HOST = "database"
DB_NAME = "buecherdb"
DB_USER = "admin"
DB_PASS = "Speechy$2026"


def get_db_connection():
    return psycopg2.connect(host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS)


def get_books_from_db():
    """Liest den lokalen Bestand inkl. aggregierter Autorennamen aus dem
    normalisierten Schema (books + book_persons + persons)."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                b.id,
                b.title,
                COALESCE(
                    string_agg(DISTINCT p.name, ', ') FILTER (WHERE bp.is_primary_author),
                    'Unbekannter Autor'
                ) AS authors,
                b.year,
                b.matching_key,
                b.price
            FROM books b
            LEFT JOIN book_persons bp ON bp.book_id = b.id
            LEFT JOIN persons p ON p.id = bp.person_id
            GROUP BY b.id
            ORDER BY b.id DESC;
        """)
        books = cursor.fetchall()
        cursor.close()
        conn.close()
        return books
    except Exception as e:
        print(f"Datenbankfehler beim Lesen: {e}")
        return []


def extract_gnd_id(field) -> str:
    """Extrahiert die reine numerische GND-ID aus einem MARC-Feld.

    Subfield $0 kommt bei der DNB oft mehrfach vor, in unterschiedlicher
    Notation für dieselbe Nummer, z.B.:
      '(DE-588)118540238'                      - ISIL der GND selbst
      'https://d-nb.info/gnd/118540238'        - kanonische Linked-Data-URI
      '(DE-101)118540238'                      - DNB-interne Katalog-ISIL
    Wir speichern bewusst NICHT eine dieser Notationen 1:1, sondern nur die
    nackte Ziffernfolge - das ist die eigentliche stabile ID, der Rest ist
    nur unterschiedliche Schreibweise dafür.
    """
    for zero_el in field.findall("./{*}subfield[@code='0']"):
        text = (zero_el.text or "").strip()
        if not text:
            continue
        candidate = text.rsplit('/', 1)[-1].rsplit(')', 1)[-1].strip()
        if candidate:
            return candidate
    return ""


def search_dnb_live(author: str, title: str, year_start: str, year_end: str, max_records: int):
    cql_parts = ["MAT=books"]
    if title and title.strip():
        cql_parts.append(f'TIT="{title.strip()}"')
    if author and author.strip():
        cql_parts.append(f'ATR="{author.strip()}"')

    start_valid = year_start and year_start.strip()
    end_valid = year_end and year_end.strip()
    if start_valid and end_valid:
        cql_parts.append(f'JHR>={year_start.strip()} and JHR<={year_end.strip()}')
    elif start_valid:
        cql_parts.append(f'JHR>={year_start.strip()}')
    elif end_valid:
        cql_parts.append(f'JHR<={year_end.strip()}')

    query_string = " and ".join(cql_parts)

    url = "https://services.dnb.de/sru/dnb"
    params = {
        "version": "1.1",
        "operation": "searchRetrieve",
        "recordSchema": "MARC21-xml",
        "maximumRecords": max_records,
        "query": query_string
    }

    req = requests.models.Request('GET', url, params=params).prepare()
    generated_url = req.url

    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code != 200:
            return [], generated_url

        # Encoding absichern, bevor geparst wird - vermeidet einen harten
        # ParseError, falls die DNB gelegentlich fehlerhaft codierte
        # Zeichen ausliefert (z.B. kaputte Umlaute).
        response.encoding = response.encoding or "utf-8"
        root = ET.fromstring(response.text.encode("utf-8", errors="replace"))

        records_found = []

        # Iteration über 'recordData' (kommt pro Treffer genau einmal vor),
        # darin gezielt der eine innere MARC-'record' - siehe frühere Analyse
        # zum falschen Namespace '{http://loc.gov}record'.
        for record_data in root.findall('.//{*}recordData'):
            marc_rec = record_data.find('.//{*}record')
            if marc_rec is None:
                continue

            # DNB-ID aus Kontrollfeld tag="001" extrahieren
            dnb_id = ""
            id_el = marc_rec.find("./{*}controlfield[@tag='001']")
            if id_el is not None and id_el.text:
                dnb_id = id_el.text.strip()

            # Titel (tag="245", code="a") und Beschreibungen extrahieren
            title_text = "Ohne Titel"
            desc_text = ""
            field_245 = marc_rec.find("./{*}datafield[@tag='245']")
            if field_245 is not None:
                a_el = field_245.find("./{*}subfield[@code='a']")
                if a_el is not None and a_el.text:
                    title_text = a_el.text.strip()

                b_el = field_245.find("./{*}subfield[@code='b']")
                c_el = field_245.find("./{*}subfield[@code='c']")
                desc_parts = []
                if b_el is not None and b_el.text:
                    desc_parts.append(b_el.text.strip())
                if c_el is not None and c_el.text:
                    desc_parts.append(c_el.text.strip())
                desc_text = " | ".join(desc_parts)

            # Personen sammeln: Haupteintragung (100) + Nebeneintragungen (700),
            # jeweils mit Name, GND-ID, Rolle. Ersetzt die frühere getrennte
            # Behandlung von "author" (String) und "contributors" (Stringliste).
            persons = []

            field_100 = marc_rec.find("./{*}datafield[@tag='100']")
            if field_100 is not None:
                a_el = field_100.find("./{*}subfield[@code='a']")
                if a_el is not None and a_el.text:
                    d_el = field_100.find("./{*}subfield[@code='d']")
                    e_el = field_100.find("./{*}subfield[@code='e']")
                    persons.append({
                        "gnd_id": extract_gnd_id(field_100),
                        "name": a_el.text.strip(),
                        "birth_death": d_el.text.strip() if d_el is not None and d_el.text else "",
                        "role": e_el.text.strip() if e_el is not None and e_el.text else "Verfasser",
                        "is_primary_author": True
                    })

            for field_700 in marc_rec.findall("./{*}datafield[@tag='700']"):
                a_el = field_700.find("./{*}subfield[@code='a']")
                if a_el is not None and a_el.text:
                    d_el = field_700.find("./{*}subfield[@code='d']")
                    e_el = field_700.find("./{*}subfield[@code='e']")
                    persons.append({
                        "gnd_id": extract_gnd_id(field_700),
                        "name": a_el.text.strip(),
                        "birth_death": d_el.text.strip() if d_el is not None and d_el.text else "",
                        "role": e_el.text.strip() if e_el is not None and e_el.text else "Mitwirkender",
                        "is_primary_author": False
                    })

            # Verlag (tag="264"/"260", code="b") + Erscheinungsort (code="a")
            # + Jahr (code="c"). Verlags-GND-ID kommt ggf. separat aus "710".
            pub_name = ""
            pub_gnd_id = ""
            place_text = ""
            year_text = "0000"
            field_pub = marc_rec.find("./{*}datafield[@tag='264']")
            if field_pub is None:
                field_pub = marc_rec.find("./{*}datafield[@tag='260']")
            if field_pub is not None:
                a_sub = field_pub.find("./{*}subfield[@code='a']")
                b_sub = field_pub.find("./{*}subfield[@code='b']")
                c_sub = field_pub.find("./{*}subfield[@code='c']")
                if a_sub is not None and a_sub.text:
                    place_text = a_sub.text.strip()
                if b_sub is not None and b_sub.text:
                    pub_name = b_sub.text.strip()
                if c_sub is not None and c_sub.text:
                    year_match = re.search(r'\d{4}', c_sub.text)
                    if year_match:
                        year_text = year_match.group(0)

            field_710 = marc_rec.find("./{*}datafield[@tag='710']")
            if field_710 is not None:
                gnd_from_710 = extract_gnd_id(field_710)
                if gnd_from_710:
                    pub_gnd_id = gnd_from_710
                if not pub_name:
                    a_el = field_710.find("./{*}subfield[@code='a']")
                    if a_el is not None and a_el.text:
                        pub_name = a_el.text.strip()

            # Umfang/Seiten aus tag="300", code="a" extrahieren
            pages_text = "0"
            field_300 = marc_rec.find("./{*}datafield[@tag='300']")
            if field_300 is not None:
                pages_el = field_300.find("./{*}subfield[@code='a']")
                if pages_el is not None and pages_el.text:
                    pages_text = pages_el.text.strip()

            # ISBN aus tag="020", code="a" (Bindestriche entfernt fürs saubere Matching)
            isbn_text = ""
            field_020 = marc_rec.find("./{*}datafield[@tag='020']")
            if field_020 is not None:
                isbn_el = field_020.find("./{*}subfield[@code='a']")
                if isbn_el is not None and isbn_el.text:
                    isbn_text = re.sub(r'[^0-9Xx]', '', isbn_el.text)

            # Ausgabebezeichnung aus tag="250", code="a" (z.B. "Reprint 2020", "3. Aufl.")
            edition_text = ""
            field_250 = marc_rec.find("./{*}datafield[@tag='250']")
            if field_250 is not None:
                ed_el = field_250.find("./{*}subfield[@code='a']")
                if ed_el is not None and ed_el.text:
                    edition_text = ed_el.text.strip()

            # Reihe aus tag="490" (bzw. Fallback 830), code="a" + Bandnummer code="v"
            series_text = ""
            field_series = marc_rec.find("./{*}datafield[@tag='490']")
            if field_series is None:
                field_series = marc_rec.find("./{*}datafield[@tag='830']")
            if field_series is not None:
                s_el = field_series.find("./{*}subfield[@code='a']")
                v_el = field_series.find("./{*}subfield[@code='v']")
                if s_el is not None and s_el.text:
                    series_text = s_el.text.strip()
                    if v_el is not None and v_el.text:
                        series_text += f", {v_el.text.strip()}"

            record = {
                "id": dnb_id,
                "title": title_text,
                "year": year_text,
                "pages": pages_text,
                "description": desc_text,
                "place": place_text,
                "isbn": isbn_text,
                "asin": "",  # DNB liefert keine ASIN - Feld bleibt für manuelle Nacherfassung frei
                "edition": edition_text,
                "series": series_text,
                "persons": persons,
                "publisher": {"gnd_id": pub_gnd_id, "name": pub_name} if pub_name else None,
            }

            # Anzeige-Felder fürs Template (unverändert nutzbar wie bisher)
            primary = next((p for p in persons if p["is_primary_author"]), None)
            record["author"] = primary["name"] if primary else "Unbekannter Autor"
            record["contributors"] = [
                f"{p['name']} ({p['role']})" for p in persons if not p["is_primary_author"]
            ]
            record["publisher_name"] = pub_name or "Unbekannt"

            # Kompletten strukturierten Datensatz Base64-kodiert mitgeben, damit
            # /import ihn ohne zweiten DNB-Request oder fragilen Server-Cache
            # direkt normalisiert einspielen kann.
            payload = base64.urlsafe_b64encode(json.dumps(record).encode("utf-8")).decode("ascii")
            record["import_payload"] = payload

            records_found.append(record)
        return records_found, generated_url
    except Exception as e:
        print(f"Fehler bei Live-Abfrage: {e}")
        return [], generated_url


def find_or_create_person(cursor, gnd_id: str, name: str, birth_death: str = "") -> int:
    """Dedupe primär über gnd_id (stabile Normdaten-ID), sonst über den
    Namen. Race Conditions bei zeitgleichen Imports werden bewusst nicht
    behandelt - für dieses manuell bedienten Admin-Tool ausreichend."""
    if gnd_id:
        cursor.execute("SELECT id FROM persons WHERE gnd_id = %s;", (gnd_id,))
        row = cursor.fetchone()
        if row:
            return row[0]
    else:
        cursor.execute("SELECT id FROM persons WHERE gnd_id IS NULL AND name = %s;", (name,))
        row = cursor.fetchone()
        if row:
            return row[0]

    cursor.execute(
        "INSERT INTO persons (gnd_id, name, birth_death) VALUES (%s, %s, %s) RETURNING id;",
        (gnd_id or None, name, birth_death or None)
    )
    return cursor.fetchone()[0]


def find_or_create_publisher(cursor, gnd_id: str, name: str):
    if not name:
        return None
    if gnd_id:
        cursor.execute("SELECT id FROM publishers WHERE gnd_id = %s;", (gnd_id,))
        row = cursor.fetchone()
        if row:
            return row[0]

    cursor.execute("SELECT id, gnd_id FROM publishers WHERE name = %s;", (name,))
    row = cursor.fetchone()
    if row:
        pub_id, existing_gnd = row
        if gnd_id and not existing_gnd:
            cursor.execute("UPDATE publishers SET gnd_id = %s WHERE id = %s;", (gnd_id, pub_id))
        return pub_id

    cursor.execute(
        "INSERT INTO publishers (gnd_id, name) VALUES (%s, %s) RETURNING id;",
        (gnd_id or None, name)
    )
    return cursor.fetchone()[0]


@app.get("/import")
def import_to_db(data: str):
    """Nimmt den Base64-kodierten JSON-Datensatz aus dem Suchergebnis
    entgegen und verteilt ihn auf books / persons / publishers / book_persons."""
    try:
        record = json.loads(base64.urlsafe_b64decode(data.encode("ascii")).decode("utf-8"))
    except Exception as e:
        print(f"Fehler beim Dekodieren des Import-Datensatzes: {e}")
        return RedirectResponse(url="/", status_code=303)

    title = record.get("title", "Ohne Titel")
    year = record.get("year") or None
    try:
        year = int(year) if year and year != "0000" else None
    except ValueError:
        year = None

    pages = record.get("pages", "")
    persons = record.get("persons", [])
    publisher = record.get("publisher")

    pages_match = re.search(r'\b(\d+)\s*(?:S\.|Seiten|p\.)', pages, re.IGNORECASE)
    clean_pages = pages_match.group(1) if pages_match else "0"
    primary = next((p for p in persons if p.get("is_primary_author")), None)
    author_for_key = primary["name"] if primary else "unbekannt"
    author_clean = re.sub(r'[^a-zA-Z]', '', author_for_key).lower()[:4]
    title_clean = re.sub(r'[^a-zA-Z]', '', title).lower()[:5]
    matching_key = f"{author_clean}-{title_clean}-{year or 0}-{clean_pages}"

    default_price = 150.00

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        publisher_id = None
        if publisher:
            publisher_id = find_or_create_publisher(cursor, publisher.get("gnd_id", ""), publisher.get("name", ""))

        cursor.execute("""
            INSERT INTO books
                (dnb_id, matching_key, title, edition, series, year, place,
                 pages, description, isbn, asin, publisher_id, price, raw_description)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (dnb_id) DO NOTHING
            RETURNING id;
        """, (
            record.get("id") or None, matching_key, title,
            record.get("edition") or None, record.get("series") or None,
            year, record.get("place") or None, pages,
            record.get("description") or None, record.get("isbn") or None,
            record.get("asin") or None, publisher_id, default_price,
            f"DNB Import ID: {record.get('id', '')}, Umfang: {pages}"
        ))
        row = cursor.fetchone()

        if row:
            book_id = row[0]
            for person in persons:
                person_id = find_or_create_person(
                    cursor, person.get("gnd_id", ""), person.get("name", ""), person.get("birth_death", "")
                )
                cursor.execute("""
                    INSERT INTO book_persons (book_id, person_id, role, is_primary_author)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (book_id, person_id, role) DO NOTHING;
                """, (book_id, person_id, person.get("role", ""), bool(person.get("is_primary_author"))))

        conn.commit()
    except psycopg2.errors.UniqueViolation:
        # Zwei DNB-Katalogeinträge (unterschiedliche dnb_id) können denselben
        # Fuzzy-Match-Key ergeben (z.B. zwei Katalogisate derselben Ausgabe).
        # Das ist ein erwarteter Dedupe-Fall, kein echter Fehler - überspringen.
        if conn:
            conn.rollback()
        print(f"Import übersprungen (matching_key bereits vorhanden): {matching_key}")
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"Fehler beim DB-Import: {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

    return RedirectResponse(url="/", status_code=303)


@app.get("/")
def index(
    request: Request,
    author: str = Query("", description="Autor"),
    title: str = Query("", description="Titel"),
    year_start: str = Query("", description="Jahr von"),
    year_end: str = Query("", description="Jahr bis"),
    max_records: int = Query(10, description="Max. Datensätze")
):
    dnb_results = []
    generated_url = ""
    if author or title or year_start:
        dnb_results, generated_url = search_dnb_live(author, title, year_start, year_end, max_records)

    local_books = get_books_from_db()

    return templates.TemplateResponse(request, "index.html", {
        "author": author,
        "title": title,
        "year_start": year_start,
        "year_end": year_end,
        "dnb_results": dnb_results,
        "generated_url": generated_url,
        "local_books": local_books
    })
