"""Produktion: baut aus einem Skript ein fertiges Short (1080x1920).

Ein Skript liegt in scripts/<id>.json und sieht so aus:

{
  "id": "twitch_abc",
  "source": "https://clips.twitch.tv/...",     # URL oder lokaler Dateipfad
  "credit": "Clip: twitch.tv/streamername",
  "hook": "DAS hat keiner bemerkt 👀",          # Text oben in den ersten Sekunden
  "title": "Titel für den Upload",
  "description": "Beschreibung inkl. Quelle",
  "tags": ["gta6", "vicecity"],
  "beats": [
    {"original": true, "start": 4.0, "end": 9.0},              # Moment im Original-Ton
    {"text": "Achtet mal auf das Schild ...", "start": 6.0, "end": 8.0},
    {"text": "Das ist ein Verweis auf ...",  "start": 8.0, "end": 11.0}
  ]
}

- "original": Ausschnitt läuft mit Original-Ton, ohne Kommentar.
- "text": KI-Stimme spricht, darunter läuft der Ausschnitt (verlangsamt
  oder wiederholt, bis der Satz fertig ist), Original-Ton leise.

Aufruf:  python src/produce.py scripts/<id>.json
Testlauf ohne Internet:  TTS_DRYRUN=1 python src/produce.py ...
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
P = CONFIG["produce"]
W, H = P["width"], P["height"]


def run(cmd: list[str]) -> None:
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        sys.stderr.write(res.stderr[-3000:])
        raise SystemExit(f"Befehl fehlgeschlagen: {' '.join(cmd[:4])} ...")


def duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out)


def has_audio(path: Path) -> bool:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
         "-of", "csv=p=0", str(path)], capture_output=True, text=True,
    ).stdout.strip()
    return bool(out)


# ---------------------------------------------------------------- Quelle
def fetch_source(src: str, work: Path) -> Path:
    if Path(src).exists():
        return Path(src)
    dl = ROOT / "data" / "downloads"
    dl.mkdir(parents=True, exist_ok=True)
    import hashlib
    target = dl / f"{hashlib.sha1(src.encode()).hexdigest()[:16]}.mp4"
    if not target.exists():
        run(["yt-dlp", "-f", "mp4/best", "-o", str(target), src])
    return target


# ---------------------------------------------------------------- Stimme
async def _edge_tts(text: str, voice: str, mp3: Path) -> list[tuple[float, float, str]]:
    import edge_tts

    words: list[tuple[float, float, str]] = []
    comm = edge_tts.Communicate(text, voice, rate="+8%", boundary="WordBoundary")
    with mp3.open("wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                s = chunk["offset"] / 1e7
                words.append((s, s + chunk["duration"] / 1e7, chunk["text"]))
    return words


def tts(text: str, mp3: Path) -> list[tuple[float, float, str]]:
    """Erzeugt Sprachdatei und gibt Wort-Zeitstempel zurück."""
    ch = CONFIG["channel"]
    voice = ch["voice_de"] if ch["language"] == "de" else ch["voice_en"]
    if os.getenv("TTS_DRYRUN"):
        # Platzhalter: Ton + geschätzte Wortzeiten (ca. 2,6 Wörter/Sekunde)
        tokens = text.split()
        step = 1 / 2.6
        dur = max(len(tokens) * step, 1.0)
        run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=220:duration={dur}",
             "-af", "volume=0.15", str(mp3)])
        return [(i * step, (i + 1) * step, t) for i, t in enumerate(tokens)]
    return asyncio.run(_edge_tts(text, voice, mp3))


# ---------------------------------------------------------------- Video
FPS = 30


def layout_filter(stretch: float, crop: list | None = None) -> str:
    """Unscharfer Vollbild-Hintergrund + scharfes Bild in der Mitte.

    crop = [x, y, w, h] als Anteile (0–1), z. B. um Facecam/Chat wegzuschneiden.
    """
    c = ""
    if crop:
        x, y, w, h = crop
        c = f"crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},"
    return (
        f"[0:v]{c}setpts={stretch}*(PTS-STARTPTS),fps={FPS},split=2[a][b];"
        f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
        f"boxblur=25:5,eq=brightness=-0.15[bg];"
        f"[b]scale={W}:-2[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2-80,setsar=1[v]"
    )


def render_beat(src: Path, beat: dict, idx: int, work: Path, crop=None) -> tuple[Path, float, list]:
    start, end = float(beat["start"]), float(beat["end"])
    clip_len = end - start
    out = work / f"beat_{idx:02d}.mp4"
    src_audio = has_audio(src)

    if beat.get("original"):
        dur = clip_len
        audio = "[0:a]atrim=0:{d},asetpts=PTS-STARTPTS,aresample=44100[aout]".format(d=dur) if src_audio \
            else f"anullsrc=r=44100:cl=stereo,atrim=0:{dur}[aout]"
        run(["ffmpeg", "-y", "-ss", str(start), "-t", str(clip_len), "-i", str(src),
             "-filter_complex", layout_filter(1.0, crop) + ";" + audio,
             "-map", "[v]", "-map", "[aout]", "-t", str(dur),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-ar", "44100", "-ac", "2",
             str(out)])
        return out, dur, []

    mp3 = work / f"voice_{idx:02d}.mp3"
    words = tts(beat["text"], mp3)
    dur = duration(mp3) + 0.25
    # Ausschnitt bis max. 2x verlangsamen, danach wiederholen
    stretch = min(max(dur / clip_len, 1.0), 2.0)
    loops = int(dur / (clip_len * stretch)) + 1
    vol = P["original_audio_volume"]
    if src_audio:
        audio = (f"[0:a]atempo={max(1/stretch, 0.5)},volume={vol},aresample=44100[orig];"
                 f"[1:a]aresample=44100,volume=1.6[voice];"
                 f"[orig][voice]amix=inputs=2:duration=longest:dropout_transition=0,"
                 f"atrim=0:{dur}[aout]")
    else:
        audio = f"[1:a]aresample=44100,volume=1.6,apad,atrim=0:{dur}[aout]"
    run(["ffmpeg", "-y", "-stream_loop", str(loops), "-ss", str(start), "-t", str(clip_len), "-i", str(src),
         "-i", str(mp3),
         "-filter_complex", layout_filter(stretch, crop) + ";" + audio,
         "-map", "[v]", "-map", "[aout]", "-t", str(dur),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-ar", "44100", "-ac", "2",
         str(out)])
    return out, dur, words


# ---------------------------------------------------------------- Untertitel
def ts(sec: float) -> str:
    h, rem = divmod(max(sec, 0), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def build_ass(captions: list[tuple[float, float, str, int]], hook: str, credit: str, total: float) -> str:
    font = P["font"]
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,7,3,2,60,60,520,1
Style: Hook,{font},84,&H0000E5FF,&H00FFFFFF,&H00000000,&HB0000000,1,0,0,0,100,100,0,0,3,4,0,8,70,70,230,1
Style: Credit,{font},40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,0,0,0,0,100,100,0,0,3,3,0,2,60,60,120,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    if hook:
        lines.append(f"Dialogue: 1,{ts(0)},{ts(min(3.5, total))},Hook,,0,0,0,,{hook}")
    if credit:
        lines.append(f"Dialogue: 0,{ts(0)},{ts(total)},Credit,,0,0,0,,{credit}")
    # 3 Wörter pro Einblendung (nie über Beat-Grenzen), aktuelles Wort gelb
    groups, cur = [], []
    for word in captions:
        if cur and (len(cur) == 3 or word[3] != cur[-1][3]):
            groups.append(cur)
            cur = []
        cur.append(word)
    if cur:
        groups.append(cur)
    for group in groups:
        for j, (s, e, _, _b) in enumerate(group):
            nxt = group[j + 1][0] if j + 1 < len(group) else e
            end = min(nxt, e + 0.4) if j + 1 == len(group) else nxt
            txt = " ".join(
                ("{\\c&H00E5FF&}" + w.upper() + "{\\c&HFFFFFF&}") if k == j else w.upper()
                for k, (_, _, w, _b) in enumerate(group)
            )
            lines.append(f"Dialogue: 2,{ts(s)},{ts(end)},Cap,,0,0,0,,{txt}")
    return head + "\n".join(lines) + "\n"


# ---------------------------------------------------------------- Ablauf
def produce(script_path: Path) -> Path:
    global FPS
    script = json.loads(script_path.read_text(encoding="utf-8"))
    vid = script["id"]
    FPS = int(script.get("fps", 30))
    work = ROOT / "output" / vid / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    sources = {script["source"]: fetch_source(script["source"], work)}
    for b in script["beats"]:
        if b.get("source") and b["source"] not in sources:
            sources[b["source"]] = fetch_source(b["source"], work)
    crops = script.get("crops", {})
    used = sum(b["end"] - b["start"] for b in script["beats"])
    if used > P["max_source_seconds"] * 2:
        print(f"Warnung: {used:.1f}s Fremdmaterial – lieber kürzen", file=sys.stderr)

    parts, captions, t = [], [], 0.0
    for i, beat in enumerate(script["beats"]):
        key = beat.get("source", script["source"])
        crop = beat.get("crop") or crops.get(key) or script.get("crop")
        path, dur, words = render_beat(sources[key], beat, i, work, crop)
        parts.append(path)
        captions += [(t + s, t + e, w, i) for s, e, w in words]
        t += dur
    total = min(t, P["max_video_seconds"])

    concat = work / "concat.txt"
    concat.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
    joined = work / "joined.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(joined)])

    ass = work / "subs.ass"
    ass.write_text(build_ass(captions, script.get("hook", ""),
                             script.get("credit", "") if P.get("show_credit", True) else "", total),
                   encoding="utf-8")
    final = ROOT / "output" / vid / f"{vid}.mp4"
    run(["ffmpeg", "-y", "-i", str(joined), "-vf", f"ass={ass}", "-t", str(total),
         "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(final)])

    meta = {k: script.get(k) for k in ("id", "title", "description", "tags", "credit", "source")}
    meta["duration"] = round(total, 1)
    meta["status"] = "review"
    (final.parent / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Fertig: {final.relative_to(ROOT)} ({total:.1f}s)")
    return final


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Aufruf: python src/produce.py scripts/<id>.json")
    for arg in sys.argv[1:]:
        produce(Path(arg))
