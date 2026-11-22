# Sternsammler – automatischer GTA6-Shorts-Kanal

Kurze GTA6-Clips mit eigenem Kommentar (KI-Stimme), Erklärungen zu Story,
Easter Eggs, Tipps und Tricks. Komplett mit kostenlosen Tools.

## Ablauf

1. **Scout** (`src/scout.py`) holt zweimal täglich die stärksten Clips von Twitch
   und die meistgesehenen neuen Videos von YouTube → `data/candidates.json`.
2. **Skript** – Claude wählt die besten Kandidaten aus und schreibt pro Video ein
   Skript nach `scripts/<id>.json` (Hook, Kommentar, welche Sekunden des Clips).
3. **Produktion** (`src/produce.py`) baut daraus das fertige Short: Hochformat
   1080×1920, KI-Stimme (Edge-TTS), Untertitel Wort für Wort, Quellenangabe.
4. **Abnahme** – du schaust dir die Videos an und gibst sie frei.
5. **Upload** – freigegebene Videos gehen raus (kommt in Schritt 4).

## Regeln für Mehrwert (wichtig für Monetarisierung)

- Nie mehr als ca. 10–12 Sekunden Fremdmaterial am Stück.
- Jedes Video hat eigenen Kommentar, der etwas erklärt oder einordnet.
- Quelle steht immer im Bild und in der Beschreibung.
- Streamer, die widersprechen, kommen auf eine Sperrliste.

## Lokal ausführen

```bash
pip install -r requirements.txt     # ffmpeg muss installiert sein
cp .env.example .env                # Schlüssel eintragen
python src/scout.py
python src/produce.py scripts/<id>.json
```

Testlauf ohne Internet/Stimme: `TTS_DRYRUN=1 python src/produce.py scripts/test_demo.json`

## Schlüssel (kostenlos)

| Secret | Woher |
|---|---|
| `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` | dev.twitch.tv/console → „Anwendung registrieren“ |
| `YOUTUBE_API_KEY` | console.cloud.google.com → YouTube Data API v3 aktivieren → API-Schlüssel |

Auf GitHub unter *Settings → Secrets and variables → Actions* eintragen.
