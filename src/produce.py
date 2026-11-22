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
import re
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


def game_audio(src: Path) -> Path | None:
    """Trennt Streamer-Stimme vom Spielsound (Demucs) -> nur Ingame-Sound/Musik."""
    if not P.get("separate_game_audio", False) or not has_audio(src):
        return None
    target = src.with_suffix(".game.wav")
    if target.exists():
        return target
    tmp = src.parent / "sep"
    res = subprocess.run([sys.executable, "-m", "demucs", "--two-stems=vocals", "-n", "htdemucs",
                          "-o", str(tmp), str(src)], capture_output=True, text=True)
    out = tmp / "htdemucs" / src.stem / "no_vocals.wav"
    if res.returncode != 0 or not out.exists():
        print(f"[audio] Trennung fehlgeschlagen, nutze Original-Ton: {res.stderr[-300:]}", file=sys.stderr)
        return None
    shutil.move(str(out), target)
    shutil.rmtree(tmp, ignore_errors=True)
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


def _whisper_words(mp3: Path) -> list[tuple[float, float, str]]:
    """Wort-Zeitstempel per Whisper, falls die Stimme keine liefert."""
    from faster_whisper import WhisperModel
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(mp3), "-f", "f32le", "-ac", "1",
                          "-ar", "16000", "-"], capture_output=True, check=True).stdout
    model = WhisperModel("small", device="cpu", compute_type="int8")
    segs, _ = model.transcribe(np.frombuffer(raw, dtype=np.float32), language="de", word_timestamps=True)
    return [(w.start, w.end, w.word.strip()) for seg in segs for w in seg.words if w.word.strip()]


def _elevenlabs(text: str, mp3: Path, key: str) -> list[tuple[float, float, str]]:
    """Realistische Stimme über ElevenLabs inkl. Wort-Zeitstempel."""
    import base64
    import requests

    ch = CONFIG["channel"]
    voice = os.getenv("ELEVENLABS_VOICE_ID") or ch.get("elevenlabs_voice_id")
    body = {"text": text, "model_id": ch.get("elevenlabs_model", "eleven_multilingual_v2"),
            "voice_settings": {"stability": ch.get("elevenlabs_stability", 0.5),
                               "similarity_boost": 0.8, "style": ch.get("elevenlabs_style", 0.3),
                               "use_speaker_boost": True}}
    hdr = {"xi-api-key": key, "Content-Type": "application/json"}
    r = requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/with-timestamps",
                      headers=hdr, json=body, timeout=180)
    if r.status_code == 200:
        data = r.json()
        mp3.write_bytes(base64.b64decode(data["audio_base64"]))
        al = data.get("alignment") or data.get("normalized_alignment")
        if al:
            words, cur, start, end = [], "", None, 0.0
            for c, st, e in zip(al["characters"], al["character_start_times_seconds"],
                                al["character_end_times_seconds"]):
                if c.isspace():
                    if cur:
                        words.append((start, end, cur))
                    cur, start = "", None
                    continue
                if start is None:
                    start = st
                cur += c
                end = e
            if cur:
                words.append((start, end, cur))
            return words
    else:
        print(f"[tts] with-timestamps nicht möglich ({r.status_code}), nutze Whisper", file=sys.stderr)
        r = requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}", headers=hdr,
                          json=body, timeout=180)
        r.raise_for_status()
        mp3.write_bytes(r.content)
    return _whisper_words(mp3)


def spoken(text: str) -> str:
    """Wendet die Aussprache-Liste aus config.toml an (nur für die Stimme)."""
    for a, b in CONFIG.get("pronounce", {}).items():
        text = text.replace(a, b)
    return text


def voiceover_once(texts: list[str], work: Path) -> list[tuple[Path, list]] | None:
    """Spricht ALLE Kommentar-Sätze in einem Rutsch (natürlicher Fluss) und
    schneidet die Aufnahme danach an den Satzgrenzen in Stücke pro Beat."""
    if not texts:
        return None
    ch = CONFIG["channel"]
    key = os.getenv("ELEVENLABS_API_KEY") if os.getenv("VOICE_MODE", "draft") == "final" else None
    if not key or os.getenv("TTS_DRYRUN") or not (os.getenv("ELEVENLABS_VOICE_ID") or ch.get("elevenlabs_voice_id")):
        return None
    full = work / "voice_full.mp3"
    display = texts
    texts = [spoken(t) for t in texts]
    words = _elevenlabs(" ".join(t.strip() for t in texts), full, key)
    total = duration(full)
    counts = [len(t.split()) for t in texts]
    # Grenzen: Mitte der Pause zwischen letztem Wort von Beat i und erstem von Beat i+1
    bounds, idx = [0.0], 0
    for c in counts[:-1]:
        idx += c
        if idx >= len(words):
            break
        bounds.append((words[idx - 1][1] + words[idx][0]) / 2)
    while len(bounds) < len(texts):
        bounds.append(total)
    bounds.append(total)
    out, idx = [], 0
    for i, c in enumerate(counts):
        a, b = bounds[i], bounds[i + 1]
        piece = work / f"voice_{i:02d}.mp3"
        run(["ffmpeg", "-y", "-i", str(full), "-ss", f"{a:.3f}", "-to", f"{b:.3f}", "-c:a", "libmp3lame",
             "-q:a", "2", str(piece)])
        w = [(st - a, e - a, t) for st, e, t in words[idx:idx + c]]
        shown = display[i].split()
        if len(shown) == len(w):  # Untertitel zeigen den Originaltext (z. B. "GTA 6")
            w = [(st, e, d) for (st, e, _), d in zip(w, shown)]
        idx += c
        out.append((piece, w))
    return out


def tts(text: str, mp3: Path) -> list[tuple[float, float, str]]:
    """Erzeugt Sprachdatei und gibt Wort-Zeitstempel zurück.

    Reihenfolge: ElevenLabs (falls Schlüssel da) -> Edge-TTS (kostenlos).
    """
    ch = CONFIG["channel"]
    key = os.getenv("ELEVENLABS_API_KEY") if os.getenv("VOICE_MODE", "draft") == "final" else None
    if key and not os.getenv("TTS_DRYRUN") and (os.getenv("ELEVENLABS_VOICE_ID") or ch.get("elevenlabs_voice_id")):
        return _elevenlabs(text, mp3, key)
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


def _layout_filter(stretch: float, crop: list | None = None, mode: str = "fill", focus: float = 0.5,
                  inset: dict | None = None, facecam: list | None = None) -> str:
    """Bildaufbau im Hochformat.

    mode "fill":  Spielbild füllt ~70 % der Höhe, Ausschnitt folgt `focus`
                  (0 = links, 0.5 = Mitte, 1 = rechts), oben/unten unscharf.
    mode "fit":   altes Layout, ganzes 16:9-Bild als Streifen in der Mitte.
    crop = [x, y, w, h] als Anteile (0–1), z. B. um Facecam/Chat wegzuschneiden.
    """
    c = ""
    if crop:
        x, y, w, h = crop
        c = f"crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},"
    if mode == "split" and facecam:
        # Oben: Streamer-Webcam, unten: Gameplay (füllt die Breite)
        th = int(H * P.get("split_top", 0.40)) // 2 * 2
        bh = H - th
        fx, fy, fw, fh_ = facecam
        f = min(max(float(focus), 0.0), 1.0)
        top = (f"[a]crop=iw*{fw}:ih*{fh_}:iw*{fx}:ih*{fy},scale={W}:{th}:force_original_aspect_ratio=increase,"
               f"crop={W}:{th}[top];")
        if P.get("split_fit", True):
            # ganzes Spielbild in voller Breite, darüber/darunter unscharfer Hintergrund
            bottom = (f"[b]{c}split=2[b1][b2];[b1]scale={W}:{bh}:force_original_aspect_ratio=increase,"
                      f"crop={W}:{bh},boxblur=25:5,eq=brightness=-0.2[bb];[b2]scale={W}:-2:flags=lanczos[bf];"
                      f"[bb][bf]overlay=0:(H-h)/2[bot];")
        else:
            bottom = (f"[b]{c}scale=-2:{bh},crop='min({W},iw)':{bh}:'(iw-min({W},iw))*{f}':0,"
                      f"pad={W}:{bh}:(ow-iw)/2:0[bot];")
        # crop gilt im Splitscreen nur fürs Gameplay unten (z. B. Kanal-Logo weg), die Webcam kommt aus dem Vollbild
        base = (f"[0:v]setpts={stretch}*(PTS-STARTPTS),fps={FPS},split=2[a][b];" + top + bottom +
                f"[top][bot]vstack=2,setsar=1")
        if inset:
            px, py = inset.get("pos", [40, 290])
            return base + f"[base];[{inset['input']}:v]format=rgba[ins];[base][ins]overlay={px}:{py}:eof_action=repeat[v]"
        return base + "[v]"
    if mode == "full":
        # Vollbild 9:16: Spielbild füllt den ganzen Screen (wie virale Gaming-Shorts), scharf skaliert
        f = min(max(float(focus), 0.0), 1.0)
        base = (f"[0:v]{c}setpts={stretch}*(PTS-STARTPTS),fps={FPS},"
                f"scale=-2:{H}:flags=lanczos,crop='min({W},iw)':{H}:'(iw-min({W},iw))*{f}':0,"
                f"pad={W}:{H}:(ow-iw)/2:0,unsharp=5:5:0.6,setsar=1")
        if inset:
            px, py = inset.get("pos", [40, 290])
            return base + f"[base];[{inset['input']}:v]format=rgba[ins];[base][ins]overlay={px}:{py}:eof_action=repeat[v]"
        return base + "[v]"
    if mode == "fit":
        fg = f"[b]scale={W}:-2[fg];"
        pos = "(W-w)/2:(H-h)/2-80"
    else:
        f = min(max(float(focus), 0.0), 1.0)
        if P.get("fill_zoom"):
            # Zoom über die Breite: immer gleicher Bildanteil sichtbar (1/fill_zoom), egal wie die Quelle beschnitten ist
            zw = int(W * float(P["fill_zoom"])) // 2 * 2
            fg = (f"[b]scale={zw}:-2:flags=lanczos,crop={W}:ih:'(iw-{W})*{f}':0,unsharp=5:5:0.5[fg];")
        else:
            fh = int(H * P.get("fill_height", 0.58)) // 2 * 2
            fg = (f"[b]scale=-2:{fh},crop='min({W},iw)':{fh}:'(iw-min({W},iw))*{f}':0[fg];")
        pos = "(W-w)/2:(H-h)/2-40"
    if inset:
        # Standbild-Ausschnitt (z. B. Ortsname/Minimap) als Einblendung; Eingang "img" kommt von render_beat
        px, py = inset.get("pos", [40, 290])
        return (
            f"[0:v]{c}setpts={stretch}*(PTS-STARTPTS),fps={FPS},split=2[a][b];"
            f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
            f"boxblur=25:5,eq=brightness=-0.2[bg];"
            + fg +
            f"[bg][fg]overlay={pos}[base];[{inset['input']}:v]format=rgba[ins];"
            f"[base][ins]overlay={px}:{py}:shortest=0:eof_action=repeat,setsar=1[v]"
        )
    return (
        f"[0:v]{c}setpts={stretch}*(PTS-STARTPTS),fps={FPS},split=2[a][b];"
        f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
        f"boxblur=25:5,eq=brightness=-0.2[bg];"
        + fg +
        f"[bg][fg]overlay={pos},setsar=1[v]"
    )


def layout_filter(stretch: float, crop: list | None = None, mode: str = "fill", focus: float = 0.5,
                  inset: dict | None = None, facecam: list | None = None, masks: list | None = None) -> str:
    """Wie _layout_filter, aber vorher Bereiche (Webcam, Logos, eingebrannte Untertitel) unscharf
    überdecken statt wegschneiden -> das ganze Spielbild bleibt sichtbar. masks = [[x, y, w, h], ...]"""
    g = _layout_filter(stretch, crop, mode, focus, inset, facecam)
    if not masks:
        return g
    def chain(inp: str) -> tuple[str, str]:
        pre, cur = [], inp
        for k, (x, y, w, h) in enumerate(masks):
            pre.append(f"[{cur}]split=2[mk{k}a][mk{k}b];[mk{k}b]crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},"
                       f"boxblur=luma_radius='min(22,h/4)':luma_power=4:chroma_radius='min(10,ch/4)':chroma_power=4,eq=brightness=-0.25[mk{k}r];[mk{k}a][mk{k}r]overlay=main_w*{x}:main_h*{y}[mk{k}]")
            cur = f"mk{k}"
        return ";".join(pre), cur
    if mode == "split" and facecam:
        # Webcam oben bleibt scharf, nur das Gameplay unten wird maskiert
        pre, cur = chain("b")
        return g.replace("[top];[b]", f"[top];{pre};[{cur}]", 1)
    pre, cur = chain("0:v")
    return pre + ";" + g.replace("[0:v]", f"[{cur}]", 1)


def render_beat(src: Path, beat: dict, idx: int, work: Path, crop=None, voice=None,
                game: Path | None = None, mode: str = "fill", facecam: list | None = None,
                masks: list | None = None) -> tuple[Path, float, list]:
    mode = beat.get("layout", mode)
    focus = beat.get("focus", 0.5)
    inset = dict(beat["inset"]) if beat.get("inset") else None
    inset_args: list[str] = []
    if inset:
        ix, iy, iw_, ih_ = inset["crop"]
        width = int(inset.get("width", 520)) // 2 * 2
        png = work / f"inset_{idx:02d}.png"
        run(["ffmpeg", "-y", "-ss", str(inset.get("time", beat["start"])), "-i", str(src), "-frames:v", "1",
             "-vf", f"crop=iw*{iw_}:ih*{ih_}:iw*{ix}:ih*{iy},scale={width}:-2,"
                    f"drawbox=x=0:y=0:w=iw:h=ih:t=5:c=white@0.95", str(png)])
        inset_args = ["-loop", "1", "-i", str(png)]
    start, end = float(beat["start"]), float(beat["end"])
    clip_len = end - start
    out = work / f"beat_{idx:02d}.mp4"
    src_audio = has_audio(src)

    if beat.get("original"):
        dur = clip_len
        audio = "[0:a]atrim=0:{d},asetpts=PTS-STARTPTS,aresample=44100[aout]".format(d=dur) if src_audio \
            else f"anullsrc=r=44100:cl=stereo,atrim=0:{dur}[aout]"
        gin = []
        if beat.get("game_only") and game:
            # Know-how: nur Spielsound (Streamer-Stimme entfernt), voll laut
            gin = ["-ss", str(start), "-t", str(clip_len), "-i", str(game)]
            audio = f"[1:a]atrim=0:{dur},asetpts=PTS-STARTPTS,aresample=44100[aout]"
        if inset:
            inset["input"] = 2 if gin else 1
        run(["ffmpeg", "-y", "-ss", str(start), "-t", str(clip_len), "-i", str(src), *gin, *inset_args,
             "-filter_complex", layout_filter(1.0, crop, mode, focus, inset, facecam, masks) + ";" + audio,
             "-map", "[v]", "-map", "[aout]", "-t", str(dur),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-c:a", "aac", "-ar", "44100", "-ac", "2",
             str(out)])
        return out, dur, sub_words(beat)

    if voice:
        mp3, words = voice
        dur = duration(mp3) + 0.05
    else:
        mp3 = work / f"voice_{idx:02d}.mp3"
        words = tts(beat["text"], mp3)
        dur = duration(mp3) + 0.25
    # Ausschnitt bis max. 2x verlangsamen, danach wiederholen
    stretch = min(max(dur / clip_len, 1.0), 2.0)
    loops = int(dur / (clip_len * stretch)) + 1
    vol = P["game_audio_volume"] if game else P["original_audio_volume"]
    extra = []
    a_in = "0:a"
    if game:
        extra = ["-stream_loop", str(loops), "-ss", str(start), "-t", str(clip_len), "-i", str(game)]
        a_in = "2:a"
    if src_audio:
        audio = (f"[{a_in}]atempo={max(1/stretch, 0.5)},volume={vol},aresample=44100[orig];"
                 f"[1:a]aresample=44100,volume=1.0[voice];"
                 f"[orig][voice]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,"
                 f"alimiter=limit=0.95,atrim=0:{dur}[aout]")
    else:
        audio = f"[1:a]aresample=44100,volume=1.6,apad,atrim=0:{dur}[aout]"
    if inset:
        inset["input"] = 3 if extra else 2
    run(["ffmpeg", "-y", "-stream_loop", str(loops), "-ss", str(start), "-t", str(clip_len), "-i", str(src),
         "-i", str(mp3), *extra, *inset_args,
         "-filter_complex", layout_filter(stretch, crop, mode, focus, inset, facecam, masks) + ";" + audio,
         "-map", "[v]", "-map", "[aout]", "-t", str(dur),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-c:a", "aac", "-ar", "44100", "-ac", "2",
         str(out)])
    return out, dur, words


def make_sfx(work: Path) -> dict[str, Path]:
    """Erzeugt eigene, lizenzfreie Sound-Effekte (synthetisch, per ffmpeg)."""
    files = {"whoosh": work / "sfx_whoosh.wav", "ding": work / "sfx_ding.wav", "pop": work / "sfx_pop.wav"}
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anoisesrc=d=0.45:c=pink:a=0.6",
         "-af", "highpass=f=300,lowpass=f=5000,afade=t=in:d=0.25:curve=exp,afade=t=out:st=0.25:d=0.2,"
                "aecho=0.6:0.4:20:0.3", "-ar", "44100", "-ac", "2", str(files["whoosh"])])
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=f=1318:d=0.6", "-f", "lavfi", "-i", "sine=f=1975:d=0.6",
         "-filter_complex", "[0][1]amix=2,afade=t=out:st=0.02:d=0.55:curve=exp,volume=0.8",
         "-ar", "44100", "-ac", "2", str(files["ding"])])
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=f=600:d=0.08", "-af", "afade=t=out:d=0.08,volume=0.9",
         "-ar", "44100", "-ac", "2", str(files["pop"])])
    return files


# ---------------------------------------------------------------- Untertitel
def ts(sec: float) -> str:
    h, rem = divmod(max(sec, 0), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def sub_words(beat: dict) -> list[tuple[float, float, str]]:
    """Untertitel für Original-Ton: "subs": [[start, ende, "Text"], ...] in Quellzeit.
    Die Wörter werden gleichmäßig über die Zeitspanne verteilt (relativ zum Beat-Start)."""
    out = []
    b0, b1 = float(beat["start"]), float(beat["end"])
    for s, e, text in beat.get("subs", []):
        s, e = max(float(s), b0), min(float(e), b1)
        ws = text.split()
        if not ws or e <= s:
            continue
        step = (e - s) / len(ws)
        for k, w in enumerate(ws):
            out.append((s - b0 + k * step, s - b0 + (k + 1) * step, w))
    return out


def build_ass(captions: list[tuple[float, float, str, int]], hook: str, credit: str, total: float,
              brand: str = "", hook_margin: int = 230, hook_until: float = 3.5) -> str:
    font = P["font"]
    single = P.get("caption_style", "group") == "single"
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{P.get("caption_size", 86)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,{0 if single else 1},0,0,0,100,100,{2 if single else 0},0,1,{7 if single else 7},{3},2,60,60,{P.get("caption_margin", 520)},1
Style: Hook,{font},{P.get("hook_size", 84)},{"&H00FFFFFF" if single else "&H0000E5FF"},&H00FFFFFF,&H00000000,&HB0000000,{0 if single else 1},0,0,0,100,100,{2 if single else 0},0,{1 if single else 3},{7 if single else 4},{3 if single else 0},8,70,70,{hook_margin},1
Style: Brand,{font},38,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,6,0,1,2,0,8,60,60,95,1
Style: Credit,{font},40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,0,0,0,0,100,100,0,0,3,3,0,2,60,60,120,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    # Emojis (außerhalb der Grundebene) hat die Schrift nicht -> würden als Kästchen erscheinen
    hook = re.sub(r"\s{2,}", " ", re.sub(r"[\U00010000-\U0010FFFF\uFE0F\u200D]", "", hook or "")).strip()
    if hook:
        lines.append(f"Dialogue: 1,{ts(0)},{ts(min(hook_until, total))},Hook,,0,0,0,,{hook}")
    if brand:
        lines.append(f"Dialogue: 0,{ts(0)},{ts(total)},Brand,,0,0,0,,{brand}")
    if credit:
        lines.append(f"Dialogue: 0,{ts(0)},{ts(total)},Credit,,0,0,0,,{credit}")
    if single:
        # Ein Wort pro Einblendung, weiß mit dicker Kontur (Stil virale Gaming-Shorts)
        for j, (s, e, w, _b) in enumerate(captions):
            nxt = captions[j + 1][0] if j + 1 < len(captions) else e + 0.3
            end = min(nxt, e + 0.3) if nxt > e else nxt
            lines.append(f"Dialogue: 2,{ts(s)},{ts(max(end, s + 0.08))},Cap,,0,0,0,,{w.upper()}")
        return head + "\n".join(lines) + "\n"
    # 3 Wörter pro Einblendung (nie über Beat-Grenzen), aktuelles Wort gelb
    groups, cur = [], []
    for word in captions:
        limit = 2 if any(len(w[2]) > 11 for w in cur + [word]) else 3
        if cur and (len(cur) >= limit or word[3] != cur[-1][3] or cur[-1][2].endswith((".", "?", "!"))):
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
    games = {k: game_audio(v) for k, v in sources.items()}
    used = sum(b["end"] - b["start"] for b in script["beats"])
    if used > P["max_source_seconds"] * 2:
        print(f"Warnung: {used:.1f}s Fremdmaterial – lieber kürzen", file=sys.stderr)

    text_beats = [b["text"] for b in script["beats"] if not b.get("original")]
    voices = voiceover_once(text_beats, work) or []
    voice_iter = iter(voices)

    parts, captions, t, beat_starts = [], [], 0.0, []
    for i, beat in enumerate(script["beats"]):
        key = beat.get("source", script["source"])
        crop = beat.get("crop") or crops.get(key) or script.get("crop")
        v = next(voice_iter, None) if (voices and not beat.get("original")) else None
        path, dur, words = render_beat(sources[key], beat, i, work, crop, v, games.get(key),
                                       script.get("layout", P.get("layout", "fill")),
                                       beat.get("facecam") or script.get("facecams", {}).get(key) or script.get("facecam"),
                                       beat.get("mask") or script.get("masks", {}).get(key) or script.get("mask"))
        beat_starts.append((t, beat.get("sfx", "whoosh" if i > 0 else None)))
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
                             script.get("credit", "") if P.get("show_credit", True) else "", total,
                             script.get("brand", P.get("brand", "")),
                             int(H * P.get("split_top", 0.40)) + 40
                             if script.get("layout", P.get("layout")) == "split" else 230,
                             total if script.get("hook_full") else 3.5),
                   encoding="utf-8")
    final = ROOT / "output" / vid / f"{vid}.mp4"
    draft = os.getenv("VOICE_MODE", "draft") != "final"
    sfx_inputs, sfx_filters, labels = [], [], ["[0:a]"]
    if P.get("sfx", True):
        files = make_sfx(work)
        for n, (start, kind) in enumerate(beat_starts):
            if not kind or kind not in files or start <= 0:
                continue
            sfx_inputs += ["-i", str(files[kind])]
            k = len(sfx_inputs) // 2
            delay = max(int((start - (0.12 if kind == "whoosh" else 0)) * 1000), 0)
            sfx_filters.append(f"[{k}:a]adelay={delay}|{delay},volume={P.get('sfx_volume', 0.5)}[s{k}]")
            labels.append(f"[s{k}]")
    if len(labels) > 1:
        afilter = (";".join(sfx_filters) + ";" + "".join(labels) +
                   f"amix=inputs={len(labels)}:duration=first:normalize=0,"
                   "loudnorm=I=-14:TP=-1.5:LRA=11[aout]")
    else:
        afilter = "[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[aout]"
    run(["ffmpeg", "-y", "-i", str(joined), *sfx_inputs,
         "-filter_complex", f"[0:v]ass={ass}[vout];" + afilter,
         "-map", "[vout]", "-map", "[aout]", "-t", str(total),
         "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(final)])

    meta = {k: script.get(k) for k in ("id", "title", "description", "tags", "credit", "source")}
    meta["duration"] = round(total, 1)
    meta["status"] = "entwurf" if draft else "final"
    meta["voice"] = "kostenlos (Edge)" if draft else "Otto v3 (ElevenLabs)"
    for k in ("tiktok", "instagram"):
        if script.get(k):
            meta[k] = script[k]
    (final.parent / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Fertig: {final.relative_to(ROOT)} ({total:.1f}s)")
    return final


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Aufruf: python src/produce.py scripts/<id>.json")
    for arg in sys.argv[1:]:
        produce(Path(arg))
