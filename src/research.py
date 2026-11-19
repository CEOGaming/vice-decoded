"""Recherche: wie sehen virale Gaming-Shorts anderer Spiele aus?

Sucht pro Thema die meistgesehenen kurzen Videos des letzten Jahres und
speichert Dauer, Views, Likes, Kommentare und Titel -> data/research/shorts.json
Aufruf:  python src/research.py
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "research" / "shorts.json"
KEY = os.environ["YOUTUBE_API_KEY"]
API = "https://www.googleapis.com/youtube/v3"

TOPICS = {
    "gta5_lore": "GTA 5 secret easter egg #shorts",
    "gta5_funny": "GTA 5 funny moment streamer #shorts",
    "rdr2_lore": "Red Dead Redemption 2 easter egg lore #shorts",
    "rdr2_story": "Red Dead Redemption 2 story explained #shorts",
    "eldenring_lore": "Elden Ring lore #shorts",
    "cyberpunk_secret": "Cyberpunk 2077 secret #shorts",
    "minecraft": "Minecraft #shorts",
    "fortnite_streamer": "Fortnite streamer clip #shorts",
    "cod_funny": "Call of Duty funny #shorts",
    "gta6": "GTA 6 #shorts",
    "game_facts": "video game facts you didn't know #shorts",
}


def secs(d: str) -> int:
    m = re.fullmatch(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", d or "")
    if not m:
        return 0
    h, mi, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + s


def main() -> None:
    since = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = {}
    for key, q in TOPICS.items():
        s = requests.get(f"{API}/search", params={
            "key": KEY, "part": "id", "type": "video", "order": "viewCount", "q": q,
            "videoDuration": "short", "maxResults": 25, "publishedAfter": since,
        }, timeout=20)
        s.raise_for_status()
        ids = [i["id"]["videoId"] for i in s.json().get("items", [])]
        if not ids:
            continue
        v = requests.get(f"{API}/videos", params={
            "key": KEY, "part": "snippet,statistics,contentDetails", "id": ",".join(ids)}, timeout=20)
        v.raise_for_status()
        rows = []
        for it in v.json().get("items", []):
            st = it.get("statistics", {})
            rows.append({
                "id": it["id"], "title": it["snippet"]["title"],
                "channel": it["snippet"]["channelTitle"],
                "published": it["snippet"]["publishedAt"],
                "seconds": secs(it["contentDetails"]["duration"]),
                "views": int(st.get("viewCount", 0)),
                "likes": int(st.get("likeCount", 0)),
                "comments": int(st.get("commentCount", 0)),
            })
        result[key] = {"query": q, "videos": rows}
        print(key, len(rows))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
