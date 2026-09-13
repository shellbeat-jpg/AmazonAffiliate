"""
Einmaliges Setup-/Reset-Skript für das normalisierte Bücher-Schema.

Liest schema.sql (muss im selben Verzeichnis liegen) ein und führt es
komplett gegen die Datenbank aus. Da schema.sql "DROP TABLE IF EXISTS"
für book_persons/books/persons/publishers enthält, ist der Vorgang
wiederholt ausführbar (idempotent) - jeder Lauf setzt das Schema neu auf.

ACHTUNG: Das löscht bestehende Daten in diesen vier Tabellen
unwiderruflich. Deshalb fragt das Skript standardmäßig einmal nach,
bevor es loslegt. Für automatisierte Deploys (CI/CD, Docker-Entrypoint)
den Sicherheits-Check mit --yes überspringen:

    python init_db.py --yes

Verwendung ohne Flag (interaktiv, z.B. manuell im Container):

    python init_db.py
"""
import sys
from pathlib import Path
import psycopg2

# Dieselben Zugangsdaten wie in main.py - bei Bedarf hier und dort
# gemeinsam z.B. über Umgebungsvariablen pflegen, um Drift zu vermeiden.
DB_HOST = "database"
DB_NAME = "buecherdb"
DB_USER = "admin"
DB_PASS = "Speechy$2026"

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"


def confirm() -> bool:
    print(
        "Dies löscht die bestehenden Tabellen book_persons, books, persons\n"
        "und publishers (falls vorhanden) unwiderruflich und legt sie neu an.\n"
    )
    answer = input("Fortfahren? [y/N] ").strip().lower()
    return answer in ("y", "yes", "j", "ja")


def main() -> int:
    skip_confirm = "--yes" in sys.argv

    if not SCHEMA_FILE.exists():
        print(f"Fehler: {SCHEMA_FILE} nicht gefunden. Liegt sie im selben Verzeichnis wie dieses Skript?")
        return 1

    if not skip_confirm and not confirm():
        print("Abgebrochen, es wurde nichts verändert.")
        return 0

    schema_sql = SCHEMA_FILE.read_text(encoding="utf-8")

    conn = None
    try:
        conn = psycopg2.connect(host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS)
        cursor = conn.cursor()
        # Ein einzelner execute()-Aufruf reicht: psycopg2/PostgreSQL führen
        # mehrere durch ';' getrennte Anweisungen in einem String aus.
        cursor.execute(schema_sql)
        conn.commit()
        cursor.close()
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"Fehler beim Anlegen des Schemas: {e}")
        return 1
    finally:
        if conn:
            conn.close()

    print("Schema erfolgreich (neu) angelegt: book_persons, books, persons, publishers.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
