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
import uuid
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
#
