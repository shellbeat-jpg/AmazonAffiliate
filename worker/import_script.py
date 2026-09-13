import time
import psycopg2
import requests

# Verbindungsdaten zur PostgreSQL-Datenbank im Docker-Netzwerk
DB_HOST = "database"  # Docker matcht den Servicenamen automatisch als IP
DB_NAME = "buecherdb"
DB_USER = "admin"
DB_PASS = "Speechy$2026" # <-- Exakt das Passwort aus der docker-compose.yml!

def connect_db():
    """Wartet, bis die DB erreichbar ist und verbindet sich."""
    while True:
        try:
            conn = psycopg2.connect(host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS)
            return conn
        except psycopg2.OperationalError:
            print("Datenbank schläft noch... warte 2 Sekunden...")
            time.sleep(2)

def main():
    print("Python Import Worker gestartet!")
    conn = connect_db()
    cursor = conn.cursor()

    # 1. Dummy-Tabelle für den Buchbestand anlegen
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS books (
            id SERIAL PRIMARY KEY,
            title TEXT,
            author TEXT,
            year INT,
            matching_key TEXT UNIQUE,
            price NUMERIC
        );
    """)
    conn.commit()
    print("Tabelle 'books' erfolgreich geprüft/erstellt.")

    # 2. Einen bibliophilen Dummy-Eintrag simulieren (Goethe Faust 1808)
    # Später füttern Sie das über Ihre DNB-Schleife und Händler-CSVs
    dummy_title = "Faust. Eine Tragödie."
    dummy_author = "Goethe, Johann Wolfgang von"
    dummy_year = 1808
    dummy_key = "goet-faust-1808-365" # Unser berechneter Fuzzy-Schlüssel
    dummy_price = 450.00 # 450 Euro Premium-Segment

    try:
        cursor.execute("""
            INSERT INTO books (title, author, year, matching_key, price)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (matching_key) DO NOTHING;
        """, (dummy_title, dummy_author, dummy_year, dummy_key, dummy_price))
        conn.commit()
        print(f"Dummy-Buch erfolgreich in Datenbank hinterlegt: {dummy_title} ({dummy_price}€)")
    except Exception as e:
        print(f"Fehler beim Einfügen: {e}")

    cursor.close()
    conn.close()
    print("Import-Vorgang beendet. Worker geht in den Standby.")

if __name__ == "__main__":
    main()

