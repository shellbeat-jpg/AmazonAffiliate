-- =====================================================================
-- Bibliophiles Portal - Datenbankschema für mehrere 100.000 Titel
-- =====================================================================
-- Entwurfsentscheidungen:
--  1. "authors" und "contributors" sind zusammengelegt zu "persons",
--     weil die DNB selbst Autoren nur über die Rolle ("Verfasser")
--     von anderen Beteiligten unterscheidet (MARC-Feld 100 vs. 700
--     tragen beide ein Subfield $e mit der Rolle).
--  2. "role" steht in der Junction-Tabelle (book_persons), nicht in
--     der Personen-Stammtabelle - dieselbe Person kann in einem Buch
--     Illustrator und in einem anderen Herausgeber sein.
--  3. gnd_id ist der primäre Dedupe-Schlüssel für Personen/Verlage
--     (aus MARC-Subfield $0), Namens-Trigram-Index als Fallback für
--     Datensätze ohne Normdaten-Verknüpfung.
--  4. Volltextsuche über generierte tsvector-Spalte + GIN-Index,
--     ergänzt um pg_trgm für Teilstring-/Fuzzy-Suche (z.B. Autoren-
--     Autocomplete, Titel-Teiltreffer).
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------
-- Reset: bestehende Tabellen entfernen, falls vorhanden. Bewusst
-- destruktiv - die bisherige flache "books"-Tabelle darf laut Absprache
-- überschrieben werden. Macht dieses Skript wiederholt ausführbar
-- (idempotent), z.B. bei erneutem Deploy nach einer Schemaänderung.
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS book_persons CASCADE;
DROP TABLE IF EXISTS books CASCADE;
DROP TABLE IF EXISTS persons CASCADE;
DROP TABLE IF EXISTS publishers CASCADE;

-- ---------------------------------------------------------------------
-- persons: alle natürlichen Personen (Autoren, Illustratoren,
-- Herausgeber, Übersetzer, ...). Die Rolle steht NICHT hier, sondern
-- in book_persons - eine Person kann je nach Buch verschiedene Rollen
-- haben.
-- ---------------------------------------------------------------------
CREATE TABLE persons (
    id           BIGSERIAL PRIMARY KEY,
    gnd_id       TEXT UNIQUE,          -- z.B. '118540238' aus MARC $0, NULL wenn nicht normdatenverknüpft
    name         TEXT NOT NULL,        -- "Goethe, Johann Wolfgang von"
    birth_death  TEXT,                 -- "1749-1832" aus MARC $d, optional
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Fallback-Dedupe/Suche für Personen ohne GND-ID (Fuzzy-Matching auf Namen)
CREATE INDEX idx_persons_name_trgm ON persons USING gin (name gin_trgm_ops);

-- ---------------------------------------------------------------------
-- publishers: Verlage als eigene Entität, damit "Verlag" als
-- Suchparameter/Facette performant und ohne Tippfehler-Duplikate
-- funktioniert. Manche Verlage haben ebenfalls eine GND-ID (MARC 710),
-- ältere/kleinere Verlage oft nicht - dann nur Freitext.
-- ---------------------------------------------------------------------
CREATE TABLE publishers (
    id         BIGSERIAL PRIMARY KEY,
    gnd_id     TEXT UNIQUE,
    name       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (name)
);

CREATE INDEX idx_publishers_name_trgm ON publishers USING gin (name gin_trgm_ops);

-- ---------------------------------------------------------------------
-- books: die Kerntabelle. dnb_id ist der bevorzugte Dedupe-Schlüssel
-- für DNB-Importe, matching_key bleibt als Fallback für Titel aus
-- anderen Quellen (manuelle Erfassung, andere Kataloge) erhalten.
-- ---------------------------------------------------------------------
CREATE TABLE books (
    id              BIGSERIAL PRIMARY KEY,
    dnb_id          TEXT UNIQUE,          -- eindeutig, wenn von DNB importiert
    matching_key    TEXT UNIQUE,          -- Fallback-Dedupe für Nicht-DNB-Quellen
    title           TEXT NOT NULL,
    edition         TEXT,                 -- "Reprint 2020", "3. Aufl."
    series          TEXT,                 -- "Reclams Universal-Bibliothek, 1"
    year            SMALLINT,
    place           TEXT,                 -- Erscheinungsort
    pages           TEXT,                 -- Umfang, z.B. "XV, 89 S." (bewusst Text, da uneinheitlich)
    description     TEXT,
    isbn            TEXT,
    asin            TEXT,
    publisher_id    BIGINT REFERENCES publishers(id),
    price           NUMERIC(10,2),
    raw_description TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Volltextsuche über Titel + Beschreibung + Reihe. 'german' Konfiguration
    -- für sinnvolles Stemming (z.B. "Faust" findet auch "Fausts").
    search_vector TSVECTOR GENERATED ALWAYS AS (
        setweight(to_tsvector('german', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('german', coalesce(series, '')), 'B') ||
        setweight(to_tsvector('german', coalesce(description, '')), 'C')
    ) STORED
);

CREATE INDEX idx_books_search      ON books USING gin (search_vector);
CREATE INDEX idx_books_title_trgm  ON books USING gin (title gin_trgm_ops);
CREATE INDEX idx_books_isbn        ON books (isbn) WHERE isbn IS NOT NULL AND isbn <> '';
CREATE INDEX idx_books_asin        ON books (asin) WHERE asin IS NOT NULL AND asin <> '';
CREATE INDEX idx_books_year        ON books (year);
CREATE INDEX idx_books_publisher   ON books (publisher_id);

-- ---------------------------------------------------------------------
-- book_persons: Verknüpfung Buch <-> Person MIT Rolle.
-- Ersetzt sowohl authors_to_books als auch contributors_to_books aus
-- dem ursprünglichen Entwurf. is_primary_author entspricht MARC-Feld
-- 100 (Haupteintragung) vs. 700 (Nebeneintragung) - wichtig, um in der
-- UI "den" Autor von weiteren Beteiligten unterscheiden zu können.
-- ---------------------------------------------------------------------
CREATE TABLE book_persons (
    book_id           BIGINT NOT NULL REFERENCES books(id)   ON DELETE CASCADE,
    person_id         BIGINT NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    role              TEXT NOT NULL,        -- "Verfasser", "Illustrator", "Herausgeber", "Übersetzer", ...
    is_primary_author BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (book_id, person_id, role)
);

CREATE INDEX idx_book_persons_person ON book_persons (person_id);
CREATE INDEX idx_book_persons_role   ON book_persons (role);
CREATE INDEX idx_book_persons_book   ON book_persons (book_id);

-- =====================================================================
-- Beispiel-Queries für die sieben geforderten Suchparameter
-- =====================================================================

-- Volltext (Titel/Reihe/Beschreibung)
-- SELECT * FROM books
-- WHERE search_vector @@ websearch_to_tsquery('german', 'Faust Tragödie')
-- ORDER BY ts_rank(search_vector, websearch_to_tsquery('german', 'Faust Tragödie')) DESC;

-- Autor (nur Haupteintragung)
-- SELECT b.* FROM books b
-- JOIN book_persons bp ON bp.book_id = b.id AND bp.is_primary_author
-- JOIN persons p ON p.id = bp.person_id
-- WHERE p.name ILIKE '%goethe%';

-- Rolle (z.B. alle Bücher mit einem Illustrator)
-- SELECT DISTINCT b.* FROM books b
-- JOIN book_persons bp ON bp.book_id = b.id
-- WHERE bp.role = 'Illustrator';

-- Verlag
-- SELECT b.* FROM books b
-- JOIN publishers p ON p.id = b.publisher_id
-- WHERE p.name ILIKE '%gruyter%';

-- Titel, Jahr, ISBN, ASIN: direkte, indexierte Spalten auf books.
