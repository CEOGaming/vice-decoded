"""Gezielte Clip-Suche für Know-how-Videos.

Holt GTA-6-Twitch-Clips der letzten N Stunden (viele Seiten, alle Sprachen),
filtert die Titel nach Stichwörtern und analysiert die besten Treffer
(Download, Standbilder, Transkript) nach data/analysis/<id>/.
Trefferliste: data/search/<name>.json

Aufruf: python src/search_clips.py <name> "<wort1,wort2,...>" [stunden] [anzahl]
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze  # noqa: E402
import scout  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    name, words = sys.argv[1], [w.strip().lower() for w in sys.argv[2].split(",") if w.strip()]
    hours = int(sys.argv[3]) if len(sys.argv) > 3 else 72
    limit = int(sys.argv[4]) if len(sys.argv) > 4 else 12
    scout.load_env()
    cid, secret = os.environ["TWITCH_CLIENT_ID"], os.environ["TWITCH_CLIENT_SECRET"]
    headers = {"Client-Id": cid, "Authorization": f"Bearer {scout.twitch_token(cid, secret)}"}
    g = requests.get("https://api.twitch.tv/helix/games", params={"name": "Grand Theft Auto VI"},
                     headers=headers, timeout=30).json()["data"]
    game_id = g[0]["id"]
    raw = scout._paged_clips(headers, {"game_id": game_id, "started_at": scout.since_iso(hours)}, 25)
    pat = re.compile("|".join(re.escape(w) for w in words), re.I)
    hits = [c for c in raw if pat.search(c.get("title", ""))]
    hits.sort(key=lambda c: c.get("view_count", 0), reverse=True)
    rows = [scout._clip_row(c) for c in hits]
    out = ROOT / "data" / "search"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name}.json").write_text(json.dumps({"words": words, "scanned": len(raw), "hits": rows},
                                                 ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(raw)} Clips durchsucht, {len(rows)} Treffer")
    from faster_whisper import WhisperModel
    model = WhisperModel("small", device="cpu", compute_type="int8")
    for c in rows[:limit]:
        d = analyze.ANALYSIS / c["id"]
        if (d / "transcript.json").exists():
            continue
        video = analyze.download(c)
        if not video:
            continue
        try:
            d.mkdir(parents=True, exist_ok=True)
            analyze.frames(video, d / "frames")
            cam = analyze.detect_facecam(d / "frames")
            if cam:
                c["facecam"] = cam
            (d / "info.json").write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
            (d / "transcript.json").write_text(json.dumps(analyze.transcribe(video, model), ensure_ascii=False,
                                                          indent=2), encoding="utf-8")
            print("analysiert:", c["id"], c["creator"], c["title"][:60])
        except Exception as e:
            print(f"[suche] Fehler bei {c['id']}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
