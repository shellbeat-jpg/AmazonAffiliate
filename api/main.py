import xml.etree.ElementTree as ET
import re
import json
import base64
from urllib.parse import urlencode

from fastapi import FastAPI, Query, Request, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse
from amazon import lookup_by_keyword         
import requests
import psycopg2

from config import (
    BASE_DIR, 
    DB_HOST, 
    DB_NAME, 
    DB_USER, 
    DB_PASS,
    ALLOWED_LIMITS, 
    DEFAULT_LIMIT, 
    LIMIT_OPTIONS,
    SRU_MAX_RECORDS,
    TABLE_COLUMNS,
)

app = FastAPI()
templates = Jinja2Templates(directory=BASE_DIR)

def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASS
    )
    
    
def build_amazon_keyword(author: str, title: str, place: str, year: str) -> str:
    a = (author or "").strip()
    t = (title or "").strip()
    p = (place or "").strip()
    y = (year or "").strip()
    if p:
        return " ".join(x for x in [a, t, p] if x)
    return " ".join(x for x in [a, t, y] if x)
    
    @app.post("/amazon-preview")
    def amazon_preview(
        author: str = Form(default=""),
        title: str = Form(default=""),
        place: str = Form(default=""),
        year: str = Form(default="")
    ):
    
    keyword = build_amazon_keyword(author, title, place, year)

    try:
        data = lookup_by_keyword(keyword)
        if not data:
            return JSONResponse({
                "ok": False,
                "keyword": keyword,
                "text": "Keine Amazon-Daten gefunden.",
                "json": None
            }, status_code=200)

        return JSONResponse({
            "ok": True,
            "keyword": keyword,
            "text": f"Amazon-Suche erfolgreich für: {keyword}",
            "json": data
        })
    except Exception as e:
        return JSONResponse({
            "ok": False,
            "keyword": keyword,
            "text": f"Amazon-Fehler: {e}",
            "json": None
        }, status_code=500)  
    
    
def sanitize_limit(value: str, default: str = DEFAULT_LIMIT) -> str:
    if value is None:
        return default
    v = str(value).strip().upper()
    return v if v in ALLOWED_LIMITS else default
    
def get_books_from_db(limit_local: str = "20"):
    """Liest den lokalen Bestand inkl. aggregierter Autorennamen aus dem
    normalisierten Schema (books + book_persons + persons)."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        base_sql = """
            SELECT
                b.id,                          -- 0
                b.title,                       -- 1
                b.isbn,                        -- 2
                COALESCE(
                    string_agg(DISTINCT p.name, ', ') FILTER (WHERE bp.is_primary_author),
                    ''
                ) AS authors,                  -- 3
                b.year,                        -- 4
                b.pages,                       -- 5
                pub.name AS publisher_name,    -- 6
                b.place,                       -- 7
                b.description,                 -- 8
                b.edition,                     -- 9
                b.series,                      -- 10
                b.matching_key,                -- 11
                b.dnb_id                       -- 12
            FROM books b
            LEFT JOIN book_persons bp ON bp.book_id = b.id
            LEFT JOIN persons p ON p.id = bp.person_id
            LEFT JOIN publishers pub ON pub.id = b.publisher_id
            GROUP BY b.id, pub.name
            ORDER BY b.id DESC
        """

        safe_limit = sanitize_limit(limit_local, "20")
        if safe_limit == "ALL":
            cursor.execute(base_sql + ";")
        else:
            cursor.execute(base_sql + " LIMIT %s;", (int(safe_limit),))

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
    
def normalize_isbn_query(raw: str) -> str:
    return re.sub(r"[^0-9Xx]", "", (raw or "")).upper()
    
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


def build_redirect_url(author, title, isbn, year_start, year_end, start_record, limit_local="20", import_summary=None):
    params = {
        "author": author,
        "title": title,
        "isbn": isbn,
        "year_start": year_start,
        "year_end": year_end,
        "start_record": start_record,
        "limit_local": limit_local,
    }
    if import_summary:
        params.update(import_summary)
    return f"/?{urlencode(params)}"


def search_dnb_live(author: str, title: str, isbn: str, year_start: str, year_end: str, start_record: int):
    def _clean(s: str) -> str:
        return (s or "").strip()

    # ---- Build CQL query -----------------------------------------------------
    author_s = _clean(author)
    title_s = _clean(title)
    year_start_s = _clean(year_start)
    year_end_s = _clean(year_end)
    isbn_clean = normalize_isbn_query(isbn)

    cql_parts = ["MAT=books"]

    # ISBN search should not be over-constrained by TIT/ATR/JHR
    if isbn_clean:
        cql_parts.append(f'ISBN="{isbn_clean}"')
    else:
        if title_s:
            cql_parts.append(f'TIT="{title_s}"')
        if author_s:
            cql_parts.append(f'ATR="{author_s}"')

        if year_start_s and year_end_s:
            cql_parts.append(f'JHR within "{year_start_s} {year_end_s}"')
        elif year_start_s:
            cql_parts.append(f'JHR>="{year_start_s}"')
        elif year_end_s:
            cql_parts.append(f'JHR<="{year_end_s}"')

    cql_query = " and ".join(cql_parts)

    params = {
        "version": "1.1",
        "operation": "searchRetrieve",
        "query": cql_query,
        "recordSchema": "MARC21-xml",
        "maximumRecords": SRU_MAX_RECORDS,
        "startRecord": max(1, int(start_record or 1)),
    }

    # ---- Request -------------------------------------------------------------
    try:
        response = requests.get("https://services.dnb.de/sru/dnb", params=params, timeout=30)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"HTTP-Fehler bei DNB-Anfrage: {e}")
        return [], "", 0

    # ---- Parse XML -----------------------------------------------------------    
    try:
        root = ET.fromstring(response.text)
    except ET.ParseError as e:
        print(f"XML Parse Error: {e}")
        return [], response.url, 0
        
    total_hits = 0
    n_el = root.find(".//{*}numberOfRecords")
    if n_el is not None and (n_el.text or "").strip().isdigit():
        total_hits = int(n_el.text.strip())
        
    MARC_NS = {"marc": "http://www.loc.gov/MARC21/slim"}
    results = []

    for record in root.findall(".//marc:record", MARC_NS):
        title_v = ""
        description = ""
        raw_description = ""
        edition = ""
        series = ""
        contributors = []
        isbn_v = ""
        place = ""
        year_v = ""
        pages = ""
        dnb_id = ""
        persons = []
        publisher = None
        # 001
        dnb_control = record.find("./{*}controlfield[@tag='001']")
        if dnb_control is not None and dnb_control.text:
            dnb_id = dnb_control.text.strip()
        # 245$a title, 245$b subtitle/desc
        title_field = record.find("./{*}datafield[@tag='245']/{*}subfield[@code='a']")
        if title_field is not None and title_field.text:
            title_v = title_field.text.strip(" /:")
        # 245$a description
        desc_field = record.find("./{*}datafield[@tag='245']/{*}subfield[@code='b']")
        if desc_field is not None and desc_field.text:
            description = desc_field.text.strip(" /:")
        # 520$a raw_description
        raw_desc_el = record.find("./{*}datafield[@tag='520']/{*}subfield[@code='a']")
        if raw_desc_el is not None and raw_desc_el.text:
            raw_description = raw_desc_el.text.strip()
        elif description:
            raw_description = description
        # 250$a edition
        edition_field = record.find("./{*}datafield[@tag='250']/{*}subfield[@code='a']")
        if edition_field is not None and edition_field.text:
            edition = edition_field.text.strip(" /:")
        # 490$a series
        series_field = record.find("./{*}datafield[@tag='490']/{*}subfield[@code='a']")
        if series_field is not None and series_field.text:
            series = series_field.text.strip(" /:")
        # ISBN 020$a (all)
        isbns = []
        for sf in record.findall("./{*}datafield[@tag='020']/{*}subfield[@code='a']"):
            if sf is not None and sf.text:
                val = sf.text.strip().split(" ")[0]
                if val:
                    isbns.append(val)
        isbn_v = isbns[0] if isbns else ""
        # pages 300$a
        pages_field = record.find("./{*}datafield[@tag='300']/{*}subfield[@code='a']")
        if pages_field is not None and pages_field.text:
            pages = pages_field.text.strip()
        # 264 (fallback 260): place/publisher/year
        pub_field = record.find("./{*}datafield[@tag='264']") or record.find("./{*}datafield[@tag='260']")
        if pub_field is not None:
            place_el = pub_field.find("./{*}subfield[@code='a']")
            if place_el is not None and place_el.text:
                place = place_el.text.strip(" ,;:")

            pub_name_el = pub_field.find("./{*}subfield[@code='b']")
            if pub_name_el is not None and pub_name_el.text:
                publisher = {
                    "name": pub_name_el.text.strip(" ,;:"),
                    "gnd_id": extract_gnd_id(pub_field),
                }

            year_el = pub_field.find("./{*}subfield[@code='c']")
            if year_el is not None and year_el.text:
                m = re.search(r"(\d{4})", year_el.text)
                if m:
                    year_v = m.group(1)
        # authors 100 + contributors 700
        for field in record.findall("./{*}datafield[@tag='100']"):
            name_el = field.find("./{*}subfield[@code='a']")
            if name_el is not None and name_el.text:
                d_el = field.find("./{*}subfield[@code='d']")
                persons.append({
                    "name": name_el.text.strip(),
                    "gnd_id": extract_gnd_id(field),
                    "birth_death": d_el.text.strip() if d_el is not None and d_el.text else "",
                    "role": "autor",
                    "is_primary_author": True,
                })

        for field in record.findall("./{*}datafield[@tag='700']"):
            name_el = field.find("./{*}subfield[@code='a']")
            if name_el is not None and name_el.text:
                cname = name_el.text.strip()
                contributors.append(cname)
                role = "mitwirkender"
                rel = field.find("./{*}subfield[@code='4']")
                if rel is not None and rel.text:
                    role = rel.text.strip()
                d_el = field.find("./{*}subfield[@code='d']")
                persons.append({
                    "name": cname,
                    "gnd_id": extract_gnd_id(field),
                    "birth_death": d_el.text.strip() if d_el is not None and d_el.text else "",
                    "role": role,
                    "is_primary_author": False,
                })

        persons_text = ", ".join([p["name"] for p in persons if p.get("name")]) if persons else "–"
        publisher_text = publisher["name"] if publisher else "–"
        
        # FILTER: skip incomplete placeholder rows
        has_core = bool(title_v.strip()) and (
            bool(persons) or bool(place.strip()) or bool(year_v.strip()) or bool(isbn_v.strip())
        )
        if not has_core:
            continue

        payload = {
            "dnb_id": dnb_id,
            "title": title_v,
            "description": description,
            "raw_description": raw_description,
            "edition": edition,
            "series": series,
            "contributors": contributors,
            "isbn": isbn_v,
            "place": place,
            "year": year_v,
            "pages": pages,
            "persons": persons,
            "publisher": publisher,
        }
        data_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")

        results.append({
            "dnb_id": dnb_id,
            "title": title_v,
            "description": description,
            "edition": edition,
            "series": series,
            "contributors": contributors,
            "isbn": isbn_v,
            "place": place,
            "year": year_v,
            "pages": pages,
            "persons_text": persons_text,
            "publisher_text": publisher_text,
            "data_b64": data_b64,
        })

    return results, response.url, total_hits


@app.get("/")
def read_root(
    request: Request,
    author: str = Query(default=""),
    title: str = Query(default=""),
    isbn: str = Query(default=""),
    year_start: str = Query(default=""),
    year_end: str = Query(default=""),
    start_record: int = Query(default=1),
    limit_local: str = Query(default="20"),
    imported: int = Query(default=0),
    selected: int = Query(default=0),
    skipped: int = Query(default=0),
    failed_decode: int = Query(default=0),
):

    if start_record < 1:
        start_record = 1  
    search_results = []
    limit_local = sanitize_limit(limit_local, "20")
    
    try:
        start_record = int(start_record)
    except (TypeError, ValueError):
        start_record = 1
    if start_record < 1:
        start_record = 1
    
    debug_url = ""
    total_hits = 0
    
    active_columns = sorted(
        [c for c in TABLE_COLUMNS if c.get("visible")],
        key=lambda c: c.get("order", 9999)
    )
    
    if author or title or isbn or year_start or year_end:
        search_results, debug_url, total_hits  = search_dnb_live(author, title, isbn, year_start, year_end, start_record)
        
    range_start = 0
    range_end = 0
    prev_start = None
    next_start = None

    if total_hits > 0:
        range_start = min(start_record, total_hits)
        range_end = min(start_record + SRU_MAX_RECORDS - 1, total_hits)
        if start_record > 1:
            prev_start = max(1, start_record - SRU_MAX_RECORDS)
        if range_end < total_hits:
            next_start = start_record + SRU_MAX_RECORDS
            
    db_books = get_books_from_db(limit_local=limit_local)
    
    return templates.TemplateResponse("index.html", {
        "request": request,
        "author": author,
        "title": title,
        "isbn": isbn,
        "year_start": year_start,
        "year_end": year_end,
        "start_record": start_record,   
        "total_hits": total_hits,       
        "range_start": range_start,    
        "range_end": range_end,        
        "prev_start": prev_start,     
        "next_start": next_start,     
        "limit_local": limit_local,
        "limit_options": LIMIT_OPTIONS,
        "search_results": search_results,
        "db_books": db_books,
        "active_columns": active_columns,
        "debug_url": debug_url,
        "import_summary": {
            "imported": imported,
            "selected": selected,
            "skipped": skipped,
            "failed_decode": failed_decode,
        },
    })

@app.post("/delete-books")
def delete_books(
    selected_db_ids: list[int] = Form(default=[]),
    redirect_author: str = Form(default=""),
    redirect_title: str = Form(default=""),
    redirect_isbn: str = Form(default=""),
    redirect_year_start: str = Form(default=""),
    redirect_year_end: str = Form(default=""),
    redirect_limit_local: str = Form(default="20"),
    redirect_start_record: str = Form(default="1"),
):  

    redirect_limit_local = sanitize_limit(redirect_limit_local, "20")
    
    params = {
        "author": redirect_author,
        "title": redirect_title,
        "isbn": redirect_isbn,
        "year_start": redirect_year_start,
        "year_end": redirect_year_end,
        "limit_local": redirect_limit_local,
        "start_record": redirect_start_record,
    }

    if not selected_db_ids:
        return RedirectResponse(url=f"/?{urlencode(params)}", status_code=303)

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM book_persons WHERE book_id = ANY(%s);", (selected_db_ids,))
        cursor.execute("DELETE FROM books WHERE id = ANY(%s);", (selected_db_ids,))
        conn.commit()
    except Exception as e:
        print(f"Fehler beim Löschen von Büchern: {e}")
        if conn:
            conn.rollback()
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
            

    
    return RedirectResponse(url=f"/?{urlencode(params)}", status_code=303)
    
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
    isbn = (record.get("isbn") or "").strip() or None
    description = (record.get("description") or "").strip() or None
    raw_description = (record.get("raw_description") or "").strip() or None
    edition = (record.get("edition") or "").strip() or None
    series = (record.get("series") or "").strip() or None
    place = (record.get("place") or "").strip() or None
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
            INSERT INTO books (dnb_id, title, isbn, year, matching_key, pages, publisher_id,
                                edition, series, place, description, raw_description)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (dnb_id) DO UPDATE
            SET
                title = EXCLUDED.title,
                isbn = EXCLUDED.isbn,
                year = EXCLUDED.year,
                matching_key = EXCLUDED.matching_key,
                pages = EXCLUDED.pages,
                publisher_id = EXCLUDED.publisher_id,
                edition = COALESCE(EXCLUDED.edition, books.edition),
                series = COALESCE(EXCLUDED.series, books.series),
                place = COALESCE(EXCLUDED.place, books.place),
                description = COALESCE(EXCLUDED.description, books.description),
                raw_description = COALESCE(EXCLUDED.raw_description, books.raw_description)
            RETURNING id;
            """,
            (dnb_id, title, isbn, year, matching_key, pages_raw or None, publisher_id, 
             edition, series, place, description, raw_description)
        )
    else:
        cursor.execute(
            """                               
            INSERT INTO books (dnb_id, title, isbn, year, matching_key, pages, publisher_id, 
                                edition, series, place, description, raw_description)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (matching_key) DO UPDATE
            SET
                title = EXCLUDED.title, 
                isbn = EXCLUDED.isbn,
                year = EXCLUDED.year,
                pages = EXCLUDED.pages,
                publisher_id = EXCLUDED.publisher_id,
                edition = COALESCE(EXCLUDED.edition, books.edition),
                series = COALESCE(EXCLUDED.series, books.series),
                place = COALESCE(EXCLUDED.place, books.place),
                description = COALESCE(EXCLUDED.description, books.description),
                raw_description = COALESCE(EXCLUDED.raw_description, books.raw_description)
            RETURNING id;
            """,
            (None, title, isbn, year, matching_key, pages_raw or None, publisher_id, 
             edition, series, place, description, raw_description)
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
    redirect_isbn: str = Form(default=""),
    redirect_year_start: str = Form(default=""),
    redirect_year_end: str = Form(default=""),
    redirect_limit_local: str = Form(default="20"),
    redirect_start_record: str = Form(default="1"),
):
    """POST-Import für Mehrfachauswahl aus Suchergebnissen inkl. Zustandserhalt via RedirectResponse."""
    selected_count = len(selected_data)
    imported_count = 0
    skipped_count = 0
    failed_decode_count = 0
    redirect_limit_local = sanitize_limit(redirect_limit_local, "20")

    if not selected_data:
        url = build_redirect_url(
            redirect_author,
            redirect_title,
            redirect_isbn,
            redirect_year_start,
            redirect_year_end,
            redirect_start_record,
            redirect_limit_local,
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
        redirect_isbn,
        redirect_year_start,
        redirect_year_end,
        redirect_start_record,
        redirect_limit_local,
        {
            "imported": imported_count,
            "selected": selected_count,
            "skipped": skipped_count,
            "failed_decode": failed_decode_count,
        },
    )
    return RedirectResponse(url=url, status_code=303)
