"""Runde 2: mehr männliche deutsche Stimmen + Otto-Varianten -> data/voice_samples/r2_*.mp3"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "voice_samples"
H = {"xi-api-key": os.environ["ELEVENLABS_API_KEY"]}
API = "https://api.elevenlabs.io/v1"
TEXT = ("Wusstest du, dass in G-T-A sechs mitten auf dem Meer ein Hund auf dich warten kann? "
        "Und plötzlich sieht er das: ein Jetski, ganz allein, und darauf sitzt ein Hund.")
OTTO = os.environ["OTTO_ID"]
known = {v["voice_id"] for v in json.loads((OUT / "index.json").read_text())}


def say(voice: str, fname: str, model: str, settings: dict) -> bool:
    r = requests.post(f"{API}/text-to-speech/{voice}", headers={**H, "Content-Type": "application/json"},
                      json={"text": TEXT, "model_id": model, "voice_settings": settings}, timeout=120)
    if r.status_code != 200:
        print("übersprungen", fname, r.status_code, r.text[:200])
        return False
    (OUT / fname).write_bytes(r.content)
    print("Probe:", fname)
    return True


done = []
# Otto-Varianten
variants = [
    ("r2_01_otto_lebendig.mp3", "eleven_multilingual_v2", {"stability": 0.3, "similarity_boost": 0.8, "style": 0.55, "use_speaker_boost": True}),
    ("r2_02_otto_v3.mp3", "eleven_v3", {"stability": 0.5}),
]
for f, m, s in variants:
    if say(OTTO, f, m, s):
        done.append({"file": f, "name": "Otto", "model": m, "settings": s, "voice_id": OTTO})

r = requests.get(f"{API}/shared-voices", headers=H, timeout=30, params={
    "language": "de", "gender": "male", "page_size": 50, "sort": "usage_character_count_1y"})
r.raise_for_status()
n = 3
for v in r.json().get("voices", []):
    if n > 8:
        break
    if v["voice_id"] in known or v["voice_id"] == OTTO:
        continue
    use = ((v.get("use_case") or "") + " " + (v.get("descriptive") or "")).lower()
    if not any(k in use for k in ("conversational", "casual", "narrat", "social", "natural", "entertain")):
        continue
    slug = re.sub(r"[^a-z0-9]+", "_", v.get("name", "").lower()).strip("_")[:28]
    f = f"r2_{n:02d}_{slug}.mp3"
    s = {"stability": 0.35, "similarity_boost": 0.8, "style": 0.45, "use_speaker_boost": True}
    if say(v["voice_id"], f, "eleven_multilingual_v2", s):
        done.append({"file": f, "name": v.get("name"), "voice_id": v["voice_id"], "use_case": v.get("use_case"),
                     "descriptive": v.get("descriptive"), "age": v.get("age"), "model": "eleven_multilingual_v2", "settings": s})
        n += 1
(OUT / "index_r2.json").write_text(json.dumps(done, ensure_ascii=False, indent=2), encoding="utf-8")
