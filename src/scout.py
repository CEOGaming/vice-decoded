"""Scout: findet die stärksten GTA6-Momente der letzten Stunden.

Quellen (alle kostenlos):
- Twitch Helix API  -> meistgesehene Clips der Kategorie GTA VI
- YouTube Data API  -> meistgesehene neue Videos zum Suchbegriff

Ergebnis: data/candidates.json, nach Score sortiert.
Aufruf:  python src/scout.py
"""
from __future__ import annotations

import json
import math
import os
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CONFIG = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
OUT = ROOT / "data" / "candidates.json"
TIMEOUT = 20


def load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())


def since_iso(hours: int) -> str:
    t = datetime.now(timezone.utc) - timedelta(hours=hours)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- Twitch
def twitch_token(cid: str, secret: str) -> str:
    r = requests.post(
        "https://id.twitch.tv/oauth2/token",
        data={"client_id": cid, "client_secret": secret, "grant_type": "client_credentials"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _clip_row(c: dict) -> dict:
    return {
        "id": f"twitch_{c['id']}",
        "source": "twitch",
        "url": c["url"],
        "title": c["title"],
        "creator": c["broadcaster_name"],
        "creator_login": c.get("broadcaster_login") or c["broadcaster_name"].lower(),
        "creator_url": f"https://twitch.tv/{c['broadcaster_name']}",
        "views": c["view_count"],
        "duration": c["duration"],
        "language": c.get("language", ""),
        "created_at": c["created_at"],
        "thumbnail": c.get("thumbnail_url", ""),
    }


def _paged_clips(headers: dict, params: dict, pages: int) -> list[dict]:
    out, cursor = [], None
    for _ in range(pages):
        p = dict(params, first=100)
        if cursor:
            p["after"] = cursor
        r = requests.get("https://api.twitch.tv/helix/clips", params=p, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        body = r.json()
        out += body.get("data", [])
        cursor = body.get("pagination", {}).get("cursor")
        if not cursor:
            break
    return out


def scout_twitch(cfg: dict) -> list[dict]:
    cid, secret = os.getenv("TWITCH_CLIENT_ID"), os.getenv("TWITCH_CLIENT_SECRET")
    if not cid or not secret:
        print("[twitch] übersprungen: TWITCH_CLIENT_ID/SECRET fehlen", file=sys.stderr)
        return []
    headers = {"Client-Id": cid, "Authorization": f"Bearer {twitch_token(cid, secret)}"}
    g = requests.get("https://api.twitch.tv/helix/games",
                     params={"name": cfg["game_name"]}, headers=headers, timeout=TIMEOUT)
    g.raise_for_status()
    games = g.json().get("data", [])
    if not games:
        print(f"[twitch] Spiel '{cfg['game_name']}' nicht gefunden", file=sys.stderr)
        return []
    game_id = games[0]["id"]
    since = since_iso(cfg["hours_back"])

    raw = _paged_clips(headers, {"game_id": game_id, "started_at": since}, cfg.get("twitch_pages", 3))

    # Watchlist (z. B. deutsche Streamer): Clips direkt pro Kanal holen
    logins = [l.lower() for l in cfg.get("watchlist", [])]
    for i in range(0, len(logins), 100):
        u = requests.get("https://api.twitch.tv/helix/users", params=[("login", l) for l in logins[i:i + 100]],
                         headers=headers, timeout=TIMEOUT)
        u.raise_for_status()
        for user in u.json().get("data", []):
            for c in _paged_clips(headers, {"broadcaster_id": user["id"], "started_at": since}, 1):
                if c.get("game_id") == game_id:
                    c["_watchlist"] = True
                    raw.append(c)

    seen, out = set(), []
    for c in raw:
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        german = c.get("language") == "de" or c.get("_watchlist")
        minimum = cfg.get("min_twitch_views_de", 150) if german else cfg["min_twitch_views"]
        if c["view_count"] < minimum:
            continue
        row = _clip_row(c)
        row["german"] = bool(german)
        out.append(row)
    print(f"[twitch] {len(out)} Clips, davon deutsch: {sum(r['german'] for r in out)}")
    return out


# ---------------------------------------------------------------- YouTube
def scout_youtube(cfg: dict) -> list[dict]:
    key = os.getenv("YOUTUBE_API_KEY")
    if not key:
        print("[youtube] übersprungen: YOUTUBE_API_KEY fehlt", file=sys.stderr)
        return []
    s = requests.get(
        "https://www.googleapis.com/youtube/v3/search",
        params={
            "key": key, "part": "snippet", "type": "video", "order": "viewCount",
            "q": cfg["youtube_query"], "maxResults": cfg["youtube_max_results"],
            "publishedAfter": since_iso(cfg["hours_back"]),
        },
        timeout=TIMEOUT,
    )
    s.raise_for_status()
    ids = [i["id"]["videoId"] for i in s.json().get("items", [])]
    if not ids:
        return []
    v = requests.get(
        "https://www.googleapis.com/youtube/v3/videos",
        params={"key": key, "part": "snippet,statistics,contentDetails", "id": ",".join(ids)},
        timeout=TIMEOUT,
    )
    v.raise_for_status()
    out = []
    for it in v.json().get("items", []):
        sn, st = it["snippet"], it.get("statistics", {})
        out.append({
            "id": f"youtube_{it['id']}",
            "source": "youtube",
            "url": f"https://www.youtube.com/watch?v={it['id']}",
            "title": sn["title"],
            "creator": sn["channelTitle"],
            "creator_url": f"https://www.youtube.com/channel/{sn['channelId']}",
            "views": int(st.get("viewCount", 0)),
            "likes": int(st.get("likeCount", 0)),
            "comments": int(st.get("commentCount", 0)),
            "duration": it["contentDetails"]["duration"],
            "created_at": sn["publishedAt"],
            "description": sn.get("description", "")[:500],
        })
    return out


# ---------------------------------------------------------------- Score
def iso_seconds(d: str) -> int:
    """PT1H2M3S -> 3723"""
    import re
    m = re.fullmatch(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", d or "")
    if not m:
        return 0
    h, mi, se = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + se


def classify(c: dict) -> None:
    """clip = als Material nutzbar, trend = nur als Themen-Signal (z. B. lange Streams)."""
    if c["source"] == "youtube":
        secs = iso_seconds(c["duration"])
        c["duration_s"] = secs
        c["kind"] = "clip" if 0 < secs <= 180 else "trend"
    else:
        c["kind"] = "clip"


def score(c: dict) -> float:
    """Views pro Stunde, logarithmisch, plus Engagement-Bonus."""
    created = datetime.fromisoformat(c["created_at"].replace("Z", "+00:00"))
    hours = max((datetime.now(timezone.utc) - created).total_seconds() / 3600, 1)
    velocity = c["views"] / hours
    engagement = (c.get("likes", 0) + 3 * c.get("comments", 0)) / max(c["views"], 1)
    bonus = CONFIG["scout"].get("german_bonus", 6) if c.get("german") else 0
    return round(math.log10(velocity + 1) * 10 + engagement * 50 + bonus, 2)


def main() -> None:
    load_env()
    cfg = CONFIG["scout"]
    candidates = scout_twitch(cfg) + scout_youtube(cfg)
    for c in candidates:
        classify(c)
        c["score"] = score(c)
    # Nutzbare Clips zuerst, Trend-Signale dahinter
    candidates.sort(key=lambda c: (c["kind"] == "clip", c["score"]), reverse=True)

    seen = set()
    if OUT.exists():
        seen = {c["id"] for c in json.loads(OUT.read_text(encoding="utf-8")).get("used", [])}
    fresh = [c for c in candidates if c["id"] not in seen]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidates": fresh,
        "used": [{"id": i} for i in sorted(seen)],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(fresh)} Kandidaten -> {OUT.relative_to(ROOT)}")
    for c in fresh[:10]:
        print(f"  {c['score']:6.1f}  [{c['source']}/{c['kind']}] {c['creator']}: {c['title'][:70]}")


if __name__ == "__main__":
    main()
