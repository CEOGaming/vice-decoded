"""Analyse: lädt die besten Clips, transkribiert sie und zieht Standbilder.

Damit kann das Skript auf dem beruhen, was im Clip wirklich passiert.
Ergebnis pro Clip in data/analysis/<id>/:
  info.json        Metadaten aus dem Scout
  transcript.json  gesprochener Text mit Zeitstempeln (Whisper)
  frames/tXX.jpg   Standbild alle 2 Sekunden (klein)

Aufruf:  python src/analyze.py [anzahl]
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CANDIDATES = ROOT / "data" / "candidates.json"
ANALYSIS = ROOT / "data" / "analysis"
DOWNLOADS = ROOT / "data" / "downloads"


def download(c: dict) -> Path | None:
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    target = DOWNLOADS / f"{c['id']}.mp4"
    if target.exists():
        return target
    res = subprocess.run(["yt-dlp", "-q", "-f", "mp4/best", "-o", str(target), c["url"]],
                         capture_output=True, text=True)
    if res.returncode != 0 or not target.exists():
        print(f"[download] fehlgeschlagen: {c['url']}\n{res.stderr[-500:]}", file=sys.stderr)
        return None
    return target


def frames(video: Path, out: Path, every: float = 2.0) -> None:
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-i", str(video),
         "-vf", f"fps=1/{every},scale=480:-2", "-q:v", "5", str(out / "f%03d.jpg")],
        check=True,
    )
    # nach Sekunde umbenennen: f001 -> t00, f002 -> t02 ...
    for f in sorted(out.glob("f*.jpg")):
        n = int(f.stem[1:]) - 1
        f.rename(out / f"t{int(n * every):02d}.jpg")


def load_audio(video: Path):
    """Audio per ffmpeg als 16-kHz-Mono-Array (umgeht PyAV-Versionsprobleme)."""
    import numpy as np
    raw = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", str(video), "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.float32)


def transcribe(video: Path, model) -> dict:
    segments, info = model.transcribe(load_audio(video), vad_filter=True)
    return {
        "language": info.language,
        "segments": [{"start": round(s.start, 1), "end": round(s.end, 1), "text": s.text.strip()}
                     for s in segments],
    }


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    data = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    # YouTube blockt Downloads aus Rechenzentren -> vorerst nur Twitch-Clips als Material
    clips = [c for c in data["candidates"]
             if c.get("kind", "clip") == "clip" and c["source"] == "twitch"][:limit]
    if not clips:
        print("Keine Clips zum Analysieren.")
        return

    from faster_whisper import WhisperModel
    model = WhisperModel("small", device="cpu", compute_type="int8")

    for c in clips:
        out = ANALYSIS / c["id"]
        if (out / "transcript.json").exists():
            continue
        video = download(c)
        if not video:
            continue
        try:
            out.mkdir(parents=True, exist_ok=True)
            (out / "info.json").write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
            frames(video, out / "frames")
            (out / "transcript.json").write_text(
                json.dumps(transcribe(video, model), ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"analysiert: {c['id']}  {c['creator']}: {c['title'][:60]}")
        except Exception as e:  # ein kaputter Clip soll den Lauf nicht stoppen
            print(f"[analyse] Fehler bei {c['id']}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
