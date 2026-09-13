import xml.etree.ElementTree as ET
import re
import json
import base64
from pathlib import Path
from urllib.parse import urlencode
from fastapi import FastAPI, Query, Request, Form
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


def normalize_year(year_raw):
    if year_raw is None:
        return None
    text = str(year_raw).strip()
    if not text or text == "0000":
        return None
    m = re.search(r"(\d{4})", text)
    if not m:
        return None
    y = int(m.group(1))
    if y < 1000 or y > 2100:
        return None
    return y


def normalize_pages(pages_raw):
    text = (pages_raw or "").strip()
    if not text:
        return "0"

    # Erst bevorzugte Seitennotationen
    m = re.search(r'\b(\d+)\s*(?:S\.|Seiten|p\.)', text, re.IGNORECASE)
    if m:
        return m.group(1)

    # Fallback: erste numerische Angabe im Feld
    m2 = re.search(r'\b(\d{1,5})\b', text)
    if m2:
        return m2.group(1)

    return "0"


def build_redirect_url(author, title, year_start, year_end, max_records, import_summary=None):
    params = {
        "author": author,
        "title": title,
        "year_start": year_start,
        "year_end": year_end,
        "max_records": max_records,
    }
    if import_summary:
        params.update(import_summary)
    return f"/?{urlencode(params)}"


def search_dnb_live(author: str, title: str, year_start: str, year_end: str, max_records: int):
    cql_parts = ["MAT=books"]
    if title and title.strip():
        cql_parts.append(f'TIT="{title.strip()}"')
    if author and author.strip():
        cql_parts.append(f'ATR="{author.strip()}"')

    start_valid = year_start and year_start.strip()
    end_valid = year_end and year_end.strip()

    if start_valid and end_valid:
        cql_parts.append(f'JHR within "{year_start.strip()} {year_end.strip()}"')
    elif start_valid:
        cql_parts.append(f'JHR>="{year_start.strip()}"')
    elif end_valid:
        cql_parts.append(f'JHR<="{year_end.strip()}"')

    cql_query = " and ".join(cql_parts)

    params = {
        "version": "1.1",
        "operation": "searchRetrieve",
        "query": cql_query,
        "recordSchema": "MARC21-xml",
        "maximumRecords": max_records,
    }

    try:
        response = requests.get("https://services.dnb.de/sru/dnb", params=params, timeout=30)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"HTTP-Fehler bei DNB-Anfrage: {e}")
        return [], ""

    xml_data = response.text
    results = []

    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as e:
        print(f"XML Parse Error: {e}")
        return [], response.url

    for record in root.findall(".//{*}record"):
        title = ""
        year = ""
        pages = ""
        dnb_id = ""

        persons = []
        publisher = None

        dnb_control = record.find("./{*}controlfield[@tag='001']")
        if dnb_control is not None and dnb_control.text:
            dnb_id = dnb_control.text.strip()

        title_field = record.find("./{*}datafield[@tag='245']/{*}subfield[@code='a']")
        if title_field is not None and title_field.text:
            title = title_field.text.strip(" /:")

        pages_field = record.find("./{*}datafield[@tag='300']/{*}subfield[@code='a']")
        if pages_field is not None and pages_field.text:
            pages = pages_field.text.strip()

        year_field = record.find("./{*}datafield[@tag='264']/{*}subfield[@code='c']")
        if year_field is None:
            year_field = record.find("./{*}datafield[@tag='260']/{*}subfield[@code='c']")
        if year_field is not None and year_field.text:
            m = re.search(r'(\d{4})', year_field.text)
            if m:
                year = m.group(1)

        for field in record.findall("./{*}datafield[@tag='100']"):
            name_el = field.find("./{*}subfield[@code='a']")
            if name_el is not None and name_el.text:
                persons.append({
                    "name": name_el.text.strip(),
                    "gnd_id": extract_gnd_id(field),
                    "birth_death": (field.find("./{*}subfield[@code='d']").text.strip()
                                    if field.find("./{*}subfield[@code='d']") is not None
                                    and field.find("./{*}subfield[@code='d']").text else ""),
                    "role": "autor",
                    "is_primary_author": True,
                })

        for field in record.findall("./{*}datafield[@tag='700']"):
            name_el = field.find("./{*}subfield[@code='a']")
            if name_el is not None and name_el.text:
                role = "mitwirkender"
                rel = field.find("./{*}subfield[@code='4']")
                if rel is not None and rel.text:
                    role = rel.text.strip()
                persons.append({
                    "name": name_el.text.strip(),
                    "gnd_id": extract_gnd_id(field),
                    "birth_death": (field.find("./{*}subfield[@code='d']").text.strip()
                                    if field.find("./{*}subfield[@code='d']") is not None
                                    and field.find("./{*}subfield[@code='d']").text else ""),
                    "role": role,
                    "is_primary_author": False,
                })

        pub_field = record.find("./{*}datafield[@tag='264']")
        if pub_field is None:
            pub_field = record.find("./{*}datafield[@tag='260']")
        if pub_field is not None:
            pub_name_el = pub_field.find("./{*}subfield[@code='b']")
            if pub_name_el is not None and pub_name_el.text:
                publisher = {
                    "name": pub_name_el.text.strip(" ,;:"),
                    "gnd_id": extract_gnd_id(pub_field),
                }

        # base64-kodierte Rohdaten für Import-Button
        payload = {
            "dnb_id": dnb_id,
            "title": title,
            "year": year,
            "pages": pages,
            "persons": persons,
            "publisher": publisher,
        }
        data_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")

        results.append({
            "dnb_id": dnb_id,
            "title": title,
            "year": year,
            "pages": pages,
            "persons_text": ", ".join([p["name"] for p in persons]) if persons else "–",
            "publisher_text": publisher["name"] if publisher else "–",
            "data_b64": data_b64,
        })

    return results, response.url


@app.get("/")
def read_root(
    request: Request,
    author: str = Query(default=""),
    title: str = Query(default=""),
    year_start: str = Query(default=""),
    year_end: str = Query(default=""),
    max_records: int = Query(default=20),
    imported: int = Query(default=0),
    selected: int = Query(default=0),
    skipped: int = Query(default=0),
    failed_decode: int = Query(default=0),
):
    search_results = []
    debug_url = ""

    if author or title or year_start or year_end:
        search_results, debug_url = search_dnb_live(author, title, year_start, year_end, max_records)

    db_books = get_books_from_db()

    return templates.TemplateResponse("index.html", {
        "request": request,
        "author": author,
        "title": title,
        "year_start": year_start,
        "year_end": year_end,
        "max_records": max_records,
        "search_results": search_results,
        "db_books": db_books,
        "debug_url": debug_url,
        "import_summary": {
            "imported": imported,
            "selected": selected,
            "skipped": skipped,
            "failed_decode": failed_decode,
        },
    })


def find_or_create_person(cursor, gnd_id: str, name: str, birth_death: str):
    """Sucht Person anhand gnd_id (falls vorhanden), sonst Name-only fallback.
    Ohne UNIQUE(name) könnte Name-only theoretisch Mehrdeutigkeiten erzeugen,
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


def import_single_record(cursor, record: dict):
    title = (record.get("title") or "Ohne Titel").strip() or "Ohne Titel"
    year = normalize_year(record.get("year"))

    pages_raw = record.get("pages", "")
    pages_norm = normalize_pages(pages_raw)

    persons = record.get("persons", [])
    publisher = record.get("publisher")
    dnb_id = (record.get("dnb_id") or "").strip() or None

    primary = next((p for p in persons if p.get("is_primary_author")), None)
    author_for_key = (primary.get("name") if primary else "unbekannt") or "unbekannt"
    author_clean = re.sub(r'[^a-zA-Z]', '', author_for_key).lower()[:4]
    title_clean = re.sub(r'[^a-zA-Z]', '', title).lower()[:5]
    matching_key = f"{author_clean}-{title_clean}-{year or 0}-{pages_norm}"

    default_price = 150.00

    publisher_id = None
    if publisher:
        publisher_id = find_or_create_publisher(
            cursor,
            publisher.get("gnd_id", ""),
            publisher.get("name", "")
        )

    # Upsert: dedupliziert über dnb_id, ansonsten matching_key
    if dnb_id:
        cursor.execute(
            """
            INSERT INTO books (dnb_id, title, year, matching_key, pages, publisher_id, price)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (dnb_id) DO UPDATE
            SET
                title = EXCLUDED.title,
                year = EXCLUDED.year,
                matching_key = EXCLUDED.matching_key,
                pages = EXCLUDED.pages,
                publisher_id = EXCLUDED.publisher_id,
                price = LEAST(books.price, EXCLUDED.price)
            RETURNING id;
            """,
            (dnb_id, title, year, matching_key, pages_raw or None, publisher_id, default_price)
        )
    else:
        cursor.execute(
            """
            INSERT INTO books (dnb_id, title, year, matching_key, pages, publisher_id, price)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (matching_key) DO UPDATE
            SET
                title = EXCLUDED.title,
                year = EXCLUDED.year,
                pages = EXCLUDED.pages,
                publisher_id = EXCLUDED.publisher_id,
                price = LEAST(books.price, EXCLUDED.price)
            RETURNING id;
            """,
            (None, title, year, matching_key, pages_raw or None, publisher_id, default_price)
        )

    book_id = cursor.fetchone()[0]

    # Duplikate innerhalb eines Records vermeiden
    seen_person_role = set()

    for p in persons:
        pname = (p.get("name") or "").strip()
        if not pname:
            continue

        role = ((p.get("role") or "autor").strip().lower()) or "autor"
        is_primary = bool(p.get("is_primary_author", False))

        dedupe_key = (pname.lower(), (p.get("gnd_id") or "").strip(), role)
        if dedupe_key in seen_person_role:
            continue
        seen_person_role.add(dedupe_key)

        person_id = find_or_create_person(
            cursor,
            p.get("gnd_id", ""),
            pname,
            p.get("birth_death", "")
        )

        cursor.execute(
            """
            INSERT INTO book_persons (book_id, person_id, role, is_primary_author)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (book_id, person_id, role) DO UPDATE
            SET is_primary_author = EXCLUDED.is_primary_author;
            """,
            (book_id, person_id, role, is_primary)
        )


@app.post("/import")
def import_to_db(
    selected_data: list[str] = Form(default=[]),
    redirect_author: str = Form(default=""),
    redirect_title: str = Form(default=""),
    redirect_year_start: str = Form(default=""),
    redirect_year_end: str = Form(default=""),
    redirect_max_records: int = Form(default=20),
):
    """POST-Import für Mehrfachauswahl aus Suchergebnissen inkl. Zustandserhalt via RedirectResponse."""
    selected_count = len(selected_data)
    imported_count = 0
    skipped_count = 0
    failed_decode_count = 0

    if not selected_data:
        url = build_redirect_url(
            redirect_author,
            redirect_title,
            redirect_year_start,
            redirect_year_end,
            redirect_max_records,
            {
                "imported": imported_count,
                "selected": selected_count,
                "skipped": skipped_count,
                "failed_decode": failed_decode_count,
            },
        )
        return RedirectResponse(url=url, status_code=303)

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        for data in selected_data:
            try:
                record = json.loads(base64.urlsafe_b64decode(data.encode("ascii")).decode("utf-8"))
            except Exception as e:
                print(f"Fehler beim Dekodieren eines Import-Datensatzes: {e}")
                failed_decode_count += 1
                continue

            try:
                import_single_record(cursor, record)
                imported_count += 1
            except Exception as row_err:
                print(f"Fehler beim Import eines Datensatzes: {row_err}")
                skipped_count += 1

        conn.commit()
    except Exception as e:
        print(f"Fehler beim Bulk-Import: {e}")
        if conn:
            conn.rollback()
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

    url = build_redirect_url(
        redirect_author,
        redirect_title,
        redirect_year_start,
        redirect_year_end,
        redirect_max_records,
        {
            "imported": imported_count,
            "selected": selected_count,
            "skipped": skipped_count,
            "failed_decode": failed_decode_count,
        },
    )
    return RedirectResponse(url=url, status_code=303)
