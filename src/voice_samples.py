"""Erzeugt Stimmproben mit ElevenLabs -> data/voice_samples/*.mp3 + index.json

Sucht deutsche Erzähler-Stimmen in der Stimmen-Bibliothek und nimmt zusätzlich
die Standard-Stimmen des Kontos. Pro Stimme wird derselbe Testsatz gesprochen.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "voice_samples"
KEY = os.environ["ELEVENLABS_API_KEY"]
H = {"xi-api-key": KEY}
API = "https://api.elevenlabs.io/v1"
TEXT = ("Wusstest du, dass in G-T-A sechs mitten auf dem Meer ein Hund auf dich warten kann? "
        "Und plötzlich sieht er das: ein Jetski, ganz allein, und darauf sitzt ein Hund.")
MAX = int(os.getenv("MAX_SAMPLES", "8"))


def candidates() -> list[dict]:
    found = []
    try:
        r = requests.get(f"{API}/shared-voices", headers=H, timeout=30, params={
            "language": "de", "page_size": 30, "sort": "usage_character_count_1y"})
        r.raise_for_status()
        for v in r.json().get("voices", []):
            use = (v.get("use_case") or "") + " " + (v.get("descriptive") or "")
            if any(k in use.lower() for k in ("narrat", "social", "entertain", "conversational", "casual", "informative")):
                found.append({"voice_id": v["voice_id"], "name": v.get("name", ""),
                              "gender": v.get("gender", ""), "age": v.get("age", ""),
                              "accent": v.get("accent", ""), "use_case": v.get("use_case", ""),
                              "public_owner_id": v.get("public_owner_id"), "library": True})
    except Exception as e:
        print("Bibliothek nicht verfügbar:", e)
    try:
        r = requests.get(f"{API}/voices", headers=H, timeout=30)
        r.raise_for_status()
        for v in r.json().get("voices", []):
            lab = v.get("labels", {})
            found.append({"voice_id": v["voice_id"], "name": v.get("name", ""),
                          "gender": lab.get("gender", ""), "age": lab.get("age", ""),
                          "accent": lab.get("accent", ""), "use_case": lab.get("use_case", ""),
                          "library": False})
    except Exception as e:
        print("Eigene Stimmen nicht verfügbar:", e)
    return found


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    done = []
    for v in candidates():
        if len(done) >= MAX:
            break
        r = requests.post(f"{API}/text-to-speech/{v['voice_id']}", headers={**H, "Content-Type": "application/json"},
                          json={"text": TEXT, "model_id": "eleven_multilingual_v2",
                                "voice_settings": {"stability": 0.45, "similarity_boost": 0.8, "style": 0.35}},
                          timeout=120)
        if r.status_code != 200:
            print("übersprungen", v["name"], r.status_code, r.text[:150])
            continue
        slug = re.sub(r"[^a-z0-9]+", "_", v["name"].lower()).strip("_")[:30] or v["voice_id"]
        fname = f"{len(done) + 1:02d}_{slug}.mp3"
        (OUT / fname).write_bytes(r.content)
        v["file"] = fname
        done.append(v)
        print("Probe:", fname, v["gender"], v["accent"], v["use_case"])
    (OUT / "index.json").write_text(json.dumps(done, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
