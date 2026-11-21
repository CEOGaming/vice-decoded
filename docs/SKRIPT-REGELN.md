# Skript-Regeln für Vice Decoded

Gilt für jedes Skript in `scripts/<id>.json`. Sprache: Deutsch. Stimme: Otto (ElevenLabs v3).

## Video-Typen (Mix pro Tag)
- **Reaktion / viraler Stream-Moment** (20–40 s): lustige, verrückte, zufällige Momente aus Streams. Kurz erklären, was passiert, 1–2 kurze Original-Reaktionen (je max. ~5 s).
- **Witzige Fakten / Wusstest du schon** (30–60 s): ein Detail aus dem Clip, eingeordnet.
- **Know-how / So geht's / Wo finde ich was** (45–75 s): konkrete Anleitung, Ort im Spiel (Ortsname per `inset` als Standbild einblenden), Schritte zum Nachmachen.
- Deutsche Streamer bevorzugen (Watchlist in `config.toml`).

## Aufbau
1. **Erste Sekunde = Höhepunkt** (stärkstes Bild + Hook-Satz). Kein Vorlauf.
2. Kontext: wer, wo, was passiert.
3. Original-Reaktion des Streamers (kurz, `"original": true`).
4. Mehrwert: Fakt, Erklärung oder Anleitung.
5. Abschluss mit Frage an die Zuschauer.

## Wahrheit
- Nur behaupten, was im Clip zu sehen/hören ist (Transkript, Standbilder, `inspect.yml`) oder per Web-Quelle belegt ist. Unsicheres als unsicher sagen.
- Keine Leaks, keine Spoiler im Hook, Story-Spoiler nur mit Hinweis.

## Technik
- `layout`: `fill` (Standard) oder `split` (oben Webcam, unten Gameplay) – `split` nur, wenn `facecam` in `data/analysis/<id>/info.json` steht; dann `"facecam": [...]` ins Skript übernehmen.
- `focus` pro Beat (0 links … 1 rechts), damit das Wichtige im Bild ist.
- `crop`, um Chat/Overlays wegzuschneiden (nicht bei `split`).
- Texte normal schreiben („GTA 6“). Aussprache regelt `[pronounce]` in `config.toml`.
- Fremdmaterial am Stück kurz halten; eigener Kommentar ist der Hauptteil.
- Pflichtfelder: `id`, `source`, `hook`, `title` (mit #shorts), `description` (mit Quelle + „Fan-Kanal, nicht mit Rockstar Games verbunden.“ + 3–5 Hashtags), `tags`, `beats`.
- Zusätzlich `tiktok` und `instagram` (Caption-Text) für das Upload-Paket.
