"""
Vinted Vintage-Alert-Bot
------------------------
Durchsucht Vinted regelmäßig nach definierten Suchbegriffen (z.B. Marke,
Modell), filtert nach Preis/Zustand und schickt bei neuen Treffern eine
Telegram-Push-Nachricht mit Direktlink zum Inserat.

WICHTIG:
- Dieses Skript kauft NICHTS automatisch. Es benachrichtigt dich nur,
  damit du selbst schnell zuschlagen kannst.
- Bitte nicht zu aggressiv pollen (siehe POLL_INTERVAL_SECONDS) -
  respektiere die Nutzungsbedingungen von Vinted.
"""

import json
import os
import time
import sqlite3
import requests
from pathlib import Path

# ---------------------------------------------------------------------------
# KONFIGURATION
# ---------------------------------------------------------------------------

# Werden bei GitHub Actions aus den Repository-Secrets befüllt.
# Für lokalen Betrieb kannst du hier auch direkt Strings eintragen.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "DEIN_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "DEINE_CHAT_ID")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Nur Artikel in diesen Zuständen werden akzeptiert (wie von Vinted vergeben).
# Alles darunter (z.B. "Zufriedenstellend") fliegt automatisch raus.
ACCEPTED_CONDITIONS = {
    "Neu, mit Etikett",
    "Neu, ohne Etikett",
    "Sehr guter Zustand",
}

# Keywords, die auf ein Fake/Replik hindeuten - werden bei JEDER Suche
# zusätzlich zu den individuellen "exclude"-Keywords geprüft.
FAKE_KEYWORDS = [
    "replik", "replica", "fake", "inspired by", "nach vorbild",
    "1:1", "aaa", "stil von", "style", "unofficial", "bootleg",
    "nicht original", "nachbau",
]

# Zustands-Keywords, die auf sichtbare Mängel hindeuten - fliegen ebenfalls raus.
DAMAGE_KEYWORDS = [
    "beschädigt", "damaged", "fleck", "flecken", "loch", "löcher",
    "riss", "rissig", "verblasst", "abgenutzt", "defekt", "mängel",
]

# Optionale KI-Bildprüfung: lässt Claude die Produktfotos gegenchecken
# (Logo-Qualität, Nähte, Etiketten, sichtbare Schäden). Kostet pro Treffer
# einen API-Call, dafür deutlich zuverlässiger als reine Keyword-Filter.
# Wird automatisch aktiv, sobald ANTHROPIC_API_KEY gesetzt ist (Secret).
ENABLE_AI_PHOTO_CHECK = bool(ANTHROPIC_API_KEY)

# Deine Suchaufträge. Jeder Eintrag ist eine eigene Vinted-Suche.
# "query"      -> Suchbegriff wie im Vinted-Suchfeld
# "max_price"  -> maximaler Preis in Euro
# "exclude"    -> zusätzliche Keywords, die im Titel NICHT vorkommen dürfen
SEARCHES = [
    {
        "query": "Polo Ralph Lauren Big Pony",
        "max_price": 25,
        "exclude": [],
    },
    {
        "query": "Nike Windbreaker Vintage",
        "max_price": 20,
        "exclude": [],
    },
    # Weitere Suchen hier einfach ergänzen:
    # {"query": "...", "max_price": 30, "exclude": [...]},
]

DB_PATH = Path(__file__).parent / "seen_items.db"

VINTED_SEARCH_URL = "https://www.vinted.de/api/v2/catalog/items"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


# ---------------------------------------------------------------------------
# DATENBANK (verhindert doppelte Alerts für bereits gesehene Artikel)
# ---------------------------------------------------------------------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS seen (item_id TEXT PRIMARY KEY, seen_at INTEGER)"
    )
    conn.commit()
    return conn


def already_seen(conn, item_id):
    cur = conn.execute("SELECT 1 FROM seen WHERE item_id = ?", (item_id,))
    return cur.fetchone() is not None


def mark_seen(conn, item_id):
    conn.execute(
        "INSERT OR IGNORE INTO seen (item_id, seen_at) VALUES (?, ?)",
        (item_id, int(time.time())),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# VINTED-SUCHE
# ---------------------------------------------------------------------------

def search_vinted(query, max_price):
    params = {
        "search_text": query,
        "price_to": max_price,
        "order": "newest_first",
        "per_page": 20,
    }
    try:
        resp = requests.get(VINTED_SEARCH_URL, params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("items", [])
    except requests.RequestException as e:
        print(f"[Fehler] Suche '{query}' fehlgeschlagen: {e}")
        return []


def item_matches(item, exclude_keywords):
    """Grober Text-Filter: individuelle Ausschluss-Keywords + Fake- und
    Schadens-Keywords, jeweils in Titel UND Beschreibung geprüft."""
    text = f"{item.get('title', '')} {item.get('description', '')}".lower()
    all_excludes = list(exclude_keywords) + FAKE_KEYWORDS + DAMAGE_KEYWORDS
    return not any(bad.lower() in text for bad in all_excludes)


def condition_ok(item):
    """Prüft den von Vinted vergebenen Zustand gegen ACCEPTED_CONDITIONS."""
    status = item.get("status") or item.get("condition") or ""
    if not ACCEPTED_CONDITIONS:
        return True
    return status in ACCEPTED_CONDITIONS


def ai_photo_check(item):
    """Optionale Zweitprüfung per Claude Vision: schaut sich die Fotos an
    und bewertet Echtheits-Hinweise (Logo, Nähte, Etiketten) sowie
    sichtbare Schäden. Gibt True zurück, wenn der Artikel unauffällig ist.
    Läuft nur, wenn ENABLE_AI_PHOTO_CHECK = True gesetzt ist."""
    if not ENABLE_AI_PHOTO_CHECK:
        return True

    photo_urls = [p.get("url") for p in item.get("photos", []) if p.get("url")][:4]
    if not photo_urls:
        return True  # keine Fotos vorhanden -> keine Bildprüfung möglich

    content = [
        {
            "type": "text",
            "text": (
                "Du prüfst ein Vintage-Kleidungsstück für den Weiterverkauf. "
                "Schau dir die Fotos an und beurteile knapp: "
                "1) Gibt es Hinweise auf ein Fake/Replik (Logo-Qualität, "
                "Nähte, Etiketten, Schriftart)? "
                "2) Gibt es sichtbare Schäden (Flecken, Löcher, Risse, "
                "starke Abnutzung)? "
                "Antworte NUR mit einem Wort: 'OK' wenn beides unauffällig "
                "ist, sonst 'VERDÄCHTIG'."
            ),
        }
    ]
    for url in photo_urls:
        try:
            img_resp = requests.get(url, timeout=10)
            img_resp.raise_for_status()
            import base64
            b64 = base64.b64encode(img_resp.content).decode()
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img_resp.headers.get("Content-Type", "image/jpeg"),
                    "data": b64,
                },
            })
        except requests.RequestException:
            continue

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 20,
                "messages": [{"role": "user", "content": content}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        answer = resp.json()["content"][0]["text"].strip().upper()
        return "OK" in answer
    except (requests.RequestException, KeyError, IndexError) as e:
        print(f"[Warnung] KI-Bildprüfung fehlgeschlagen, Item wird trotzdem gemeldet: {e}")
        return True


# ---------------------------------------------------------------------------
# TELEGRAM-ALERT
# ---------------------------------------------------------------------------

def send_telegram_alert(item):
    title = item.get("title", "Unbekanntes Item")
    price = item.get("price", {}).get("amount", "?")
    currency = item.get("price", {}).get("currency_code", "EUR")
    url = f"https://www.vinted.de/items/{item.get('id')}"

    text = (
        f"🎯 Neuer Treffer!\n\n"
        f"{title}\n"
        f"Preis: {price} {currency}\n"
        f"{url}"
    )

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}

    try:
        requests.post(api_url, json=payload, timeout=10)
    except requests.RequestException as e:
        print(f"[Fehler] Telegram-Alert fehlgeschlagen: {e}")


# ---------------------------------------------------------------------------
# HAUPTSCHLEIFE
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# HAUPTLAUF (ein Durchlauf pro Aufruf - GitHub Actions übernimmt den Zeitplan)
# ---------------------------------------------------------------------------

def run():
    conn = init_db()
    print("Vinted-Bot: Durchlauf gestartet.")

    for search in SEARCHES:
        items = search_vinted(search["query"], search["max_price"])
        for item in items:
            item_id = str(item.get("id"))
            if already_seen(conn, item_id):
                continue
            mark_seen(conn, item_id)

            if not item_matches(item, search["exclude"]):
                continue
            if not condition_ok(item):
                continue
            if not ai_photo_check(item):
                print(f"KI-Check: verdächtig, übersprungen -> {item.get('title')}")
                continue

            send_telegram_alert(item)
            print(f"Alert gesendet: {item.get('title')}")

    print("Vinted-Bot: Durchlauf beendet.")


if __name__ == "__main__":
    run()
