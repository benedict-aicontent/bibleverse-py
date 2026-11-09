#!/usr/bin/env python3
"""
make_videos.py  -  Bible-verse Shorts batch builder
====================================================
Reads Batch.md (7 days) and builds one 1080x1920 / 30fps video per day:

  * voiceover  = Emotional Hook + Bible Verse (with reference) + Reflection (free offline TTS)
  * video      = voiceover length (incl. pauses) + 10 s, voice starts at 2 s
  * background = ONE image for the whole video, or video clips trimmed 4s/4s/4s/8s
  * text       = whole text blocks (hook, verse, reflection) shown while spoken
  * music      = random track per video (no repeats in a batch), half the voice
                 volume and ducked further while the voice speaks
  * output     = output/DayN_Reference.mp4  +  output/DayN_Reference.txt (title/description/tags)

Run:   python make_videos.py
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time
import traceback
import wave
from pathlib import Path

try:
    from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont
except ImportError:  # handled in preflight
    Image = None

ROOT = Path(__file__).resolve().parent

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}

# --------------------------------------------------------------------------- #
# Default settings (override any of them in config.json)
# --------------------------------------------------------------------------- #
DEFAULTS = {
    "batch_file": "Batch.md",
    "backgrounds_dir": "backgrounds",
    "music_dir": "music",
    "output_dir": "output",
    "work_dir": "work",
    "look_file": "look.json",
    "font_dirs": [],                      # extra folders searched for fonts

    "width": 1080,
    "height": 1920,
    "fps": 30,

    "lead_in": 2.0,                       # voice starts at 2 s
    "tail": 8.0,                          # extra seconds after the voice ends (2 + 8 = +10 s)
    "pause": 0.6,                         # pause between hook / verse / reflection
    "reference_pause": 0.9,                # pause between the reference ("James 5, verse 15") and the verse text
    "voice_speed": 1.0,                   # 0.9 = slower, 1.1 = faster (applied with ffmpeg)
    "speak_reference": True,              # read "James 5, verse 15" before the verse
    "show_translation": True,             # show "(WEB)" next to the reference on screen
    "pronunciations": {},                 # e.g. {"Yahweh": "Yah-weh"} (spoken text only)

    "tts": {
        "engine": "piper",                # "piper" (recommended) or "windows" (built-in SAPI voices)
        "piper_model": "voices/en_US-ryan-high.onnx",
        "piper_cmd": None,                # default: [python, "-m", "piper"]
        "windows_voice_contains": "",     # e.g. "David" or "Zira"
        "windows_rate": 165,
    },

    "music": {
        "relative_volume": 0.5,           # music = half of the voice volume
        "duck": True,                     # drop further while the voice speaks
        "duck_threshold": 0.03,
        "duck_ratio": 3.0,
        "fade_in": 1.0,
        "fade_out": 4.0,
        "voice_lufs": -16,
    },

    "clips": {
        "pattern": [4, 4, 4, 8],          # trim lengths, repeated
        "zoom_min_len": 8,                # trims this long (or longer) get the slow zoom + transition
        "zoom_max": 1.15,
        "transition_duration": 0.6,
        "transitions": ["fade", "wipeleft", "wiperight", "slideleft", "slideright",
                        "circleopen", "dissolve", "fadeblack", "smoothleft", "smoothright"],
    },
    "image_slow_zoom": False,             # optional slow zoom on a still-image background

    "text": {
        "fade": 0.3,
        "start_early": 0.15,              # text appears slightly before the words
        "last_hold": 1.5,                 # last block stays this long after the voice ends
        "center_y": 0.46,                 # vertical centre (keeps clear of Shorts buttons)
        "margin_x": 90,
        "max_height_ratio": 0.62,
        "line_spacing": 1.28,
        "min_size": 34,
        "sizes": {"hook": 88, "verse": 68, "reflection": 60},
    },

    "encode": {"crf": 18, "preset": "veryfast", "audio_bitrate": "192k"},
}

LOG_FH = None


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def log(msg: str = "") -> None:
    print(msg, flush=True)
    if LOG_FH:
        LOG_FH.write(msg + "\n")
        LOG_FH.flush()


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def rpath(p) -> Path:
    p = Path(p)
    return p if p.is_absolute() else ROOT / p


def run_ffmpeg(args, desc: str = "") -> None:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"] + [str(a) for a in args]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({desc}):\n{r.stderr[-2000:]}")


def probe_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        return float(r.stdout.strip().splitlines()[0])
    except Exception:
        raise RuntimeError(f"Could not read duration of {path}: {r.stderr.strip()}")


def list_files(folder: Path, exts: set) -> list[Path]:
    if not folder.exists():
        return []
    return sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in exts)


def slugify_heading(heading: str) -> str:
    s = heading.strip().title().replace(":", "-")
    s = re.sub(r"[^A-Za-z0-9\-]+", "_", s).strip("_")
    return s


# --------------------------------------------------------------------------- #
# Batch.md parsing
# --------------------------------------------------------------------------- #
DAY_RE = re.compile(r"^#\s+DAY\s+(\d+)\s*[\u2014\u2013-]\s*(.+?)\s*$", re.M | re.I)
SEC_RE = re.compile(r"^###\s+\d+\.\s*(.+?)\s*$", re.M)


def clean_md(s: str) -> str:
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"\1", s)
    return s.strip()


def collapse(s: str) -> str:
    return re.sub(r"\s+", " ", clean_md(s)).strip()


def parse_batch(text: str) -> list[dict]:
    days = []
    matches = list(DAY_RE.finditer(text))
    if not matches:
        raise ValueError("No '# DAY N — REFERENCE' headings found in the batch file.")
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end():end]
        body = re.sub(r"^\s*---\s*$", "", body, flags=re.M)

        sections: dict[str, str] = {}
        secs = list(SEC_RE.finditer(body))
        for j, sm in enumerate(secs):
            send = secs[j + 1].start() if j + 1 < len(secs) else len(body)
            sections[sm.group(1).strip().lower()] = body[sm.end():send].strip()

        def find(prefix: str) -> str:
            for k, v in sections.items():
                if k.startswith(prefix):
                    return v
            return ""

        hook = collapse(find("emotional hook"))
        verse_raw = find("bible verse")
        reflection = collapse(find("reflection"))

        ref_line, verse_lines = "", []
        for line in verse_raw.splitlines():
            line = line.strip()
            if not line:
                continue
            if not ref_line and re.match(r"^\*\*.+\*\*$", line):
                ref_line = clean_md(line)
            else:
                verse_lines.append(line)
        verse = collapse(" ".join(verse_lines))
        if not ref_line:                                   # fall back to the heading
            ref_line = m.group(2).strip().title()
        ref_plain = re.sub(r"\s*\(.*?\)\s*$", "", ref_line).strip()

        day = {
            "num": int(m.group(1)),
            "heading": m.group(2).strip(),
            "hook": hook,
            "ref_display": ref_line,
            "ref_plain": ref_plain,
            "verse": verse,
            "reflection": reflection,
            "title": collapse(find("youtube title")),
            "description": clean_md(find("youtube description")),
            "tags": collapse(find("youtube tags")),
        }
        missing = [k for k in ("hook", "verse", "reflection") if not day[k]]
        if missing:
            raise ValueError(f"DAY {day['num']}: missing {', '.join(missing)} in the batch file.")
        days.append(day)
    return days


ORDINALS = {"1": "First", "2": "Second", "3": "Third"}


def spoken_reference(ref: str) -> str:
    """'1 Peter 2:24' -> 'First Peter 2, verse 24'"""
    m = re.match(r"^(?:(\d)\s+)?([A-Za-z][A-Za-z ]*?)\s+(\d+)(?::(\d+)(?:\s*[-\u2013]\s*(\d+))?)?$", ref.strip())
    if not m:
        return ref
    n, book, ch, v1, v2 = m.groups()
    out = (ORDINALS.get(n, n) + " " if n else "") + book + " " + ch
    if v1:
        out += f", verse {v1}" if not v2 else f", verses {v1} through {v2}"
    return out


# --------------------------------------------------------------------------- #
# Text-to-speech (free, offline)
# --------------------------------------------------------------------------- #
def tts_synthesize(text: str, out_wav: Path, cfg: dict) -> None:
    t = cfg["tts"]
    engine = t["engine"].lower()
    if engine == "piper":
        model = rpath(t["piper_model"])
        base = t["piper_cmd"] or [sys.executable, "-m", "piper"]
        cmd = list(base) + ["-m", str(model), "-f", str(out_wav)]
        r = subprocess.run(cmd, input=text.encode("utf-8"), capture_output=True)
        if r.returncode != 0 or not out_wav.exists():
            raise RuntimeError("Piper failed:\n" + r.stderr.decode("utf-8", "replace")[-1500:])
    elif engine == "windows":
        import pyttsx3  # pip install pyttsx3
        eng = pyttsx3.init()
        eng.setProperty("rate", int(t["windows_rate"]))
        want = t["windows_voice_contains"].lower()
        if want:
            for v in eng.getProperty("voices"):
                if want in v.name.lower():
                    eng.setProperty("voice", v.id)
                    break
        eng.save_to_file(text, str(out_wav))
        eng.runAndWait()
        eng.stop()
        if not out_wav.exists():
            raise RuntimeError("Windows TTS produced no file.")
    else:
        raise ValueError(f"Unknown tts engine: {engine}")


SR = 44100


def read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() == SR
        return w.readframes(w.getnframes())


def build_voiceover(day: dict, cfg: dict, work: Path) -> dict:
    """Returns timing info and writes work/voice.wav (lead-in silence included).

    Spoken parts: hook | [reference] pause | verse text | reflection.
    The reference ("James 5, verse 15") gets its own pause before the verse text.
    """
    def fix(text: str) -> str:
        for a, b in cfg["pronunciations"].items():
            text = text.replace(a, b)
        return text

    # (name, text, block index, pause after)
    pause, rpause = float(cfg["pause"]), float(cfg["reference_pause"])
    parts = [("hook", day["hook"], 0, pause)]
    if cfg["speak_reference"]:
        parts.append(("ref", spoken_reference(day["ref_plain"]), 1, rpause))
    parts.append(("verse", day["verse"], 1, pause))
    parts.append(("reflection", day["reflection"], 2, 0.0))

    speed = float(cfg["voice_speed"])
    chunks = []
    for name, text, _, _ in parts:
        raw = work / f"{name}_raw.wav"
        norm = work / f"{name}.wav"
        tts_synthesize(fix(text), raw, cfg)
        af = f"atempo={speed}" if abs(speed - 1.0) > 1e-3 else "anull"
        run_ffmpeg(["-i", raw, "-af", af, "-ar", SR, "-ac", 1, "-c:a", "pcm_s16le", norm], f"tts {name}")
        chunks.append(read_wav(norm))

    lead = float(cfg["lead_in"])
    silence = lambda s: b"\x00\x00" * int(round(s * SR))
    starts, ends, t = [None] * 3, [None] * 3, lead
    pcm = silence(lead)
    for (name, _, blk, gap), data in zip(parts, chunks):
        if starts[blk] is None:
            starts[blk] = t
        pcm += data
        t += len(data) / 2 / SR
        ends[blk] = t
        if gap:
            pcm += silence(gap)
            t += gap
    total = t + float(cfg["tail"])                       # = lead + voice (incl. pauses) + tail

    mixed = work / "voice_mixed.wav"
    with wave.open(str(mixed), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)
    lufs = cfg["music"]["voice_lufs"]
    run_ffmpeg(["-i", mixed, "-af", f"loudnorm=I={lufs}:TP=-1.5:LRA=11,aresample={SR}",
                "-ar", SR, "-ac", 1, "-c:a", "pcm_s16le", work / "voice.wav"], "voice loudness")
    return {"starts": starts, "ends": ends, "total": total, "voice_wav": work / "voice.wav",
            "voice_end": t}


# --------------------------------------------------------------------------- #
# Look (fonts / colours / styles)
# --------------------------------------------------------------------------- #
def font_search_dirs(cfg: dict) -> list[Path]:
    dirs = [ROOT / "fonts"] + [rpath(d) for d in cfg["font_dirs"]]
    windir = os.environ.get("WINDIR", r"C:\Windows")
    dirs.append(Path(windir) / "Fonts")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    return dirs


def find_font(spec, dirs: list[Path]):
    for name in ([spec] if isinstance(spec, str) else spec):
        p = Path(name)
        if p.is_absolute() and p.exists():
            return p
        for d in dirs:
            if (d / name).exists():
                return d / name
    return None


def load_look(cfg: dict) -> dict:
    data = json.loads(rpath(cfg["look_file"]).read_text(encoding="utf-8"))
    dirs = font_search_dirs(cfg)
    font_sets = []
    for fs in data["font_sets"]:
        resolved = {k: find_font(fs[k], dirs) for k in ("hook", "verse", "body")}
        if all(resolved.values()):
            font_sets.append({"name": fs.get("name", "?"), **resolved})
    if not font_sets:
        log("WARNING: none of the fonts in look.json were found - using a basic fallback font.")
        font_sets = [{"name": "fallback", "hook": None, "verse": None, "body": None}]
    data["_font_sets"] = font_sets
    return data


def pick_look(look: dict) -> dict:
    d = look.get("dim", {"min": 0.3, "max": 0.45})
    return {
        "fonts": random.choice(look["_font_sets"]),
        "colors": random.choice(look["color_sets"]),
        "style": random.choice(look["styles"]),
        "dim": random.uniform(d["min"], d["max"]),
    }


def get_font(path, size: int):
    try:
        if path:
            return ImageFont.truetype(str(path), size)
        return ImageFont.load_default(size)
    except Exception:
        return ImageFont.load_default()


def wrap_text(font, text: str, max_w: int) -> list[str]:
    lines, cur = [], ""
    for w in text.split():
        trial = (cur + " " + w).strip()
        if font.getlength(trial) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render_text_png(parts, kind: str, look: dict, cfg: dict, out: Path) -> tuple[int, int]:
    """parts = [(text, 'ref'|'main'), ...] -> transparent 1080x1920 PNG."""
    W, H = cfg["width"], cfg["height"]
    tc = cfg["text"]
    style, colors, fonts = look["style"], look["colors"], look["fonts"]
    max_w = W - 2 * tc["margin_x"]
    max_h = H * tc["max_height_ratio"]
    font_key = {"hook": "hook", "verse": "verse", "reflection": "body"}[kind]
    size = tc["sizes"][kind]

    while True:
        items = []
        for text, role in parts:
            if role == "ref":
                f = get_font(fonts["hook"], max(int(size * 0.72), 24))
                color = colors["accent"]
            else:
                f = get_font(fonts[font_key], size)
                color = colors["text"]
                if kind == "hook" and style.get("hook_upper"):
                    text = text.upper()
            for line in wrap_text(f, text, max_w):
                items.append((line, f, color, role))
        gap = int(size * 0.45)
        height = sum(int(f.size * tc["line_spacing"]) for _, f, _, _ in items)
        height += gap * sum(1 for _, _, _, r in items if r == "ref")
        if height <= max_h or size <= tc["min_size"]:
            break
        size -= 4

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    top = int(tc["center_y"] * H - height / 2)
    top = max(110, min(top, H - 330 - height))

    if style.get("box"):
        pad = 44
        wmax = max(f.getlength(l) for l, f, _, _ in items)
        box = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(box).rounded_rectangle(
            [(W - wmax) / 2 - pad, top - pad, (W + wmax) / 2 + pad, top + height + pad], radius=36,
            fill=(0, 0, 0, int(255 * style.get("box_opacity", 0.45))))
        img = Image.alpha_composite(img, box)

    shadow_px = int(style.get("shadow", 0))
    stroke = int(style.get("stroke", 0))
    shadow_rgb = ImageColor.getrgb(colors.get("shadow", "#000000"))
    stroke_rgb = ImageColor.getrgb(colors.get("stroke", "#000000"))

    layout, y = [], top
    for line, f, color, role in items:
        layout.append((line, f, color, (W - f.getlength(line)) / 2, y))
        y += int(f.size * tc["line_spacing"])
        if role == "ref":
            y += gap

    if shadow_px > 0:
        sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sd = ImageDraw.Draw(sh)
        for line, f, _, x, yy in layout:
            sd.text((x + shadow_px, yy + shadow_px), line, font=f, fill=shadow_rgb + (230,),
                    stroke_width=stroke)
        sh = sh.filter(ImageFilter.GaussianBlur(shadow_px * 0.9))
        img = Image.alpha_composite(img, sh)

    d = ImageDraw.Draw(img)
    for line, f, color, x, yy in layout:
        d.text((x, yy), line, font=f, fill=ImageColor.getrgb(color) + (255,),
               stroke_width=stroke, stroke_fill=stroke_rgb + (255,))
    box = img.getchannel("A").getbbox() or (0, 0, 2, 2)
    img.crop(box).save(out)                  # small PNG = much faster overlay
    return box[0], box[1]


# --------------------------------------------------------------------------- #
# Backgrounds
# --------------------------------------------------------------------------- #
class Assets:
    """Keeps track of what has been used so nothing repeats inside a batch."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        bg = rpath(cfg["backgrounds_dir"])
        self.images = list_files(bg, IMAGE_EXT)
        self.videos = list_files(bg, VIDEO_EXT)
        self.music = list_files(rpath(cfg["music_dir"]), AUDIO_EXT)
        self.vid_dur: dict[Path, float] = {}
        self.vid_use = {v: 0 for v in self.videos}
        self._img_queue: list[Path] = []
        self._music_queue: list[Path] = []

    def _next(self, queue_name: str, pool: list[Path], what: str) -> Path:
        q = getattr(self, queue_name)
        if not q:
            q.extend(random.sample(pool, len(pool)))
            if getattr(self, queue_name + "_used", False):
                log(f"  note: ran out of unused {what}; starting a new round (some will repeat).")
            setattr(self, queue_name + "_used", True)
        return q.pop()

    def next_image(self) -> Path:
        return self._next("_img_queue", self.images, "images")

    def next_music(self) -> Path:
        return self._next("_music_queue", self.music, "music tracks")

    def choose_mode(self) -> str:
        if self.images and self.videos:
            return random.choice(["image", "video"])
        return "image" if self.images else "video"

    def duration(self, v: Path) -> float:
        if v not in self.vid_dur:
            self.vid_dur[v] = probe_duration(v)
        return self.vid_dur[v]


class ClipPicker:
    """Picks clips for ONE video: never the same clip twice while unused ones remain."""

    def __init__(self, assets: Assets):
        self.a = assets
        self.used: set[Path] = set()
        self.last = None
        self.warned = False

    def pick(self, need: float):
        a = self.a
        eligible = [v for v in a.videos if a.duration(v) >= need]
        loop = False
        if not eligible:
            eligible, loop = list(a.videos), True
        fresh = [v for v in eligible if v not in self.used]
        if fresh:
            pool = fresh
        else:
            if not self.warned:
                log("  note: more trims needed than distinct clips available; some clips repeat "
                    "(with a different trim point).")
                self.warned = True
            pool = [v for v in eligible if v != self.last] or eligible
        least = min(a.vid_use[v] for v in pool)
        pool = [v for v in pool if a.vid_use[v] == least]
        clip = random.choice(pool)
        self.used.add(clip)
        self.last = clip
        a.vid_use[clip] += 1
        dur = a.duration(clip)
        start = 0.0 if loop else random.uniform(0, max(0.0, dur - need))
        return clip, start, loop


def zoom_vf(cfg: dict, frames: int) -> str:
    """Smooth slow zoom-in (supersampled so it does not shimmer)."""
    W, H, fps = cfg["width"], cfg["height"], cfg["fps"]
    zmax = float(cfg["clips"]["zoom_max"])
    return (f"scale={2 * W}:{2 * H}:force_original_aspect_ratio=increase,crop={2 * W}:{2 * H},"
            f"fps={fps},zoompan=z='1+{zmax - 1:.4f}*on/{max(frames, 1)}'"
            f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={fps},setsar=1")


def plain_vf(cfg: dict) -> str:
    W, H, fps = cfg["width"], cfg["height"], cfg["fps"]
    return f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={fps},setsar=1"


def venc(cfg: dict, crf=None) -> list:
    e = cfg["encode"]
    return ["-c:v", "libx264", "-preset", e["preset"], "-crf", crf or e["crf"], "-pix_fmt", "yuv420p"]


def build_image_background(img: Path, total: float, cfg: dict, out: Path) -> None:
    fps = cfg["fps"]
    if cfg["image_slow_zoom"]:
        vf = zoom_vf(cfg, int(total * fps))
    else:
        vf = plain_vf(cfg)
    run_ffmpeg(["-loop", 1, "-framerate", fps, "-i", img, "-t", f"{total:.3f}", "-vf", vf,
                "-an", *venc(cfg), "-r", fps, out], "image background")


def build_clip_background(total: float, cfg: dict, assets: Assets, work: Path) -> Path:
    cc = cfg["clips"]
    fps = cfg["fps"]
    T = float(cc["transition_duration"])
    pattern = [float(x) for x in cc["pattern"]]
    zoom_min = float(cc["zoom_min_len"])
    transition = random.choice(cc["transitions"])              # one transition style per video
    picker = ClipPicker(assets)
    cycle_len = sum(pattern)
    cycles = int(math.ceil(total / cycle_len))
    log(f"  clips: {cycles} cycle(s) of {'-'.join(str(int(p)) for p in pattern)}s, transition '{transition}'")

    cycle_files = []
    covered = 0.0                                   # seconds of footage planned so far
    for c in range(cycles):
        if covered >= total:
            break
        seg_files, seg_lens, is_long = [], [], []
        for si, plen in enumerate(pattern):
            if covered >= total:                    # enough footage - skip the rest of this cycle
                break
            covered += plen
            long_seg = plen >= zoom_min
            render_len = plen + (T if (long_seg and si > 0) else 0.0)   # extra T is eaten by the xfade
            clip, start, loop = picker.pick(render_len)
            seg = work / f"seg_{c}_{si}.mp4"
            frames = int(round(render_len * fps))
            vf = zoom_vf(cfg, frames) if long_seg else plain_vf(cfg)
            args = []
            if loop:
                args += ["-stream_loop", -1]
            args += ["-ss", f"{start:.3f}", "-t", f"{render_len:.3f}", "-i", clip, "-an", "-vf", vf,
                     *venc(cfg, 16), "-r", fps, seg]
            run_ffmpeg(args, f"segment {c}.{si} ({clip.name})")
            seg_files.append(seg)
            seg_lens.append(render_len)
            is_long.append(long_seg)

        # chain: hard cuts everywhere except an xfade into each long (zoom) trim
        inputs, graph = [], []
        for i, f in enumerate(seg_files):
            inputs += ["-i", f]
            # same frame rate + timebase on every piece, otherwise xfade refuses to join them
            graph.append(f"[{i}:v]setpts=PTS-STARTPTS,fps={fps},settb=AVTB[n{i}]")
        cur, cur_len = "[n0]", seg_lens[0]
        for i in range(1, len(seg_files)):
            lab = f"[x{i}]"
            if is_long[i]:
                graph.append(f"{cur}[n{i}]xfade=transition={transition}:duration={T}"
                             f":offset={cur_len - T:.3f}{lab}")
                cur_len += seg_lens[i] - T
            else:
                graph.append(f"{cur}[n{i}]concat=n=2:v=1:a=0{lab}")
                cur_len += seg_lens[i]
            cur = lab
        cyc = work / f"cycle_{c}.mp4"
        run_ffmpeg([*inputs, "-filter_complex", ";".join(graph) + f";{cur}format=yuv420p[v]",
                    "-map", "[v]", *venc(cfg, 16), "-r", fps, cyc], f"cycle {c}")
        cycle_files.append(cyc)
        for f in seg_files:
            f.unlink(missing_ok=True)

    lst = work / "cycles.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in cycle_files), encoding="utf-8")
    bg = work / "bg.mp4"
    run_ffmpeg(["-f", "concat", "-safe", 0, "-i", lst, "-c", "copy", bg], "join cycles")
    return bg


# --------------------------------------------------------------------------- #
# Final composition
# --------------------------------------------------------------------------- #
def compose(bg: Path, voice: dict, music: Path, pngs: list, windows: list, look: dict,
            cfg: dict, out_mp4: Path) -> None:
    fps = cfg["fps"]
    total = voice["total"]
    tc, mc = cfg["text"], cfg["music"]
    fade = float(tc["fade"])

    inputs = ["-i", bg]
    for p, _, _ in pngs:
        inputs += ["-loop", 1, "-framerate", fps, "-t", f"{total:.3f}", "-i", p]
    inputs += ["-i", voice["voice_wav"], "-stream_loop", -1, "-i", music]
    vo_idx = 1 + len(pngs)
    mu_idx = vo_idx + 1

    g = [f"[0:v]drawbox=x=0:y=0:w=iw:h=ih:color=black@{look['dim']:.2f}:t=fill[v0]"]
    last = "v0"
    for i, (s, e) in enumerate(windows):
        s = max(s, 0.0)
        g.append(f"[{1 + i}:v]format=rgba,fade=t=in:st={s:.3f}:d={fade}:alpha=1,"
                 f"fade=t=out:st={max(e - fade, s):.3f}:d={fade}:alpha=1[t{i}]")
        g.append(f"[{last}][t{i}]overlay=x={pngs[i][1]}:y={pngs[i][2]}"
                 f":enable='between(t,{s:.3f},{e + 0.05:.3f})':format=auto[v{i + 1}]")
        last = f"v{i + 1}"
    g.append(f"[{last}]format=yuv420p[vout]")

    fo = float(mc["fade_out"])
    g.append(f"[{vo_idx}:a]aresample={SR},aformat=sample_fmts=fltp:channel_layouts=stereo,"
             f"apad=whole_dur={total:.3f},asplit=2[vo1][vo2]")
    g.append(f"[{mu_idx}:a]aresample={SR},aformat=sample_fmts=fltp:channel_layouts=stereo,"
             f"atrim=0:{total:.3f},asetpts=PTS-STARTPTS,"
             f"loudnorm=I={mc['voice_lufs']}:TP=-1.5:LRA=11,aresample={SR},"
             f"volume={mc['relative_volume']},"
             f"afade=t=in:st=0:d={mc['fade_in']},afade=t=out:st={max(total - fo, 0):.3f}:d={fo}[mu]")
    if mc["duck"]:
        g.append(f"[mu][vo2]sidechaincompress=threshold={mc['duck_threshold']}:ratio={mc['duck_ratio']}"
                 f":attack=20:release=450:makeup=1[md]")
    else:
        g.append("[vo2]anullsink;[mu]anull[md]")
    g.append("[vo1][md]amix=inputs=2:duration=longest:normalize=0,"
             f"aformat=sample_fmts=fltp:channel_layouts=stereo[aout]")

    enc = cfg["encode"]
    run_ffmpeg([*inputs, "-filter_complex", ";".join(g), "-map", "[vout]", "-map", "[aout]",
                "-t", f"{total:.3f}", *venc(cfg), "-r", fps,
                "-c:a", "aac", "-b:a", enc["audio_bitrate"], "-ar", SR,
                "-movflags", "+faststart", out_mp4], "final composition")


# --------------------------------------------------------------------------- #
# One video
# --------------------------------------------------------------------------- #
def make_video(day: dict, cfg: dict, look_data: dict, assets: Assets, keep_temp: bool) -> Path:
    out_dir = rpath(cfg["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"Day{day['num']}_{slugify_heading(day['heading'])}"
    out_mp4 = out_dir / f"{name}.mp4"
    work = rpath(cfg["work_dir"]) / name
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    log(f"  voiceover ...")
    voice = build_voiceover(day, cfg, work)
    total = voice["total"]
    log(f"  voice ends at {voice['voice_end']:.1f}s -> video length {total:.1f}s")

    look = pick_look(look_data)
    log(f"  look: {look['fonts']['name']} / {look['colors']['name']} / {look['style']['name']}")

    mode = assets.choose_mode()
    if mode == "image":
        img = assets.next_image()
        log(f"  background: image {img.name}")
        bg = work / "bg.mp4"
        build_image_background(img, total, cfg, bg)
    else:
        log("  background: video clips")
        bg = build_clip_background(total, cfg, assets, work)

    music = assets.next_music()
    log(f"  music: {music.name}")

    ref_show = day["ref_display"] if cfg["show_translation"] else day["ref_plain"]
    blocks = [
        ("hook", [(day["hook"], "main")]),
        ("verse", [(ref_show, "ref"), (day["verse"], "main")]),
        ("reflection", [(day["reflection"], "main")]),
    ]
    pngs, windows = [], []
    tc = cfg["text"]
    gap = float(cfg["pause"]) - float(tc["start_early"])
    for i, (kind, parts) in enumerate(blocks):
        p = work / f"text_{kind}.png"
        x, y = render_text_png(parts, kind, look, cfg, p)
        pngs.append((p, x, y))
        s = voice["starts"][i] - float(tc["start_early"])
        e = voice["ends"][i] + (float(tc["last_hold"]) if i == 2 else min(0.3, max(gap, 0) * 0.5))
        windows.append((s, e))

    log("  composing final video ...")
    compose(bg, voice, music, pngs, windows, look, cfg, out_mp4)

    txt = (f"TITLE\n{day['title']}\n\nDESCRIPTION\n{day['description']}\n\nTAGS\n{day['tags']}\n")
    (out_dir / f"{name}.txt").write_text(txt, encoding="utf-8")

    if not keep_temp:
        shutil.rmtree(work, ignore_errors=True)
    return out_mp4


# --------------------------------------------------------------------------- #
# Preflight + main
# --------------------------------------------------------------------------- #
def preflight(cfg: dict, assets: Assets, batch_path: Path) -> list[str]:
    problems = []
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            problems.append(f"{tool} was not found on PATH.")
    if Image is None:
        problems.append("Pillow is missing. Run:  pip install pillow")
    if not batch_path.exists():
        problems.append(f"Batch file not found: {batch_path}")
    if not assets.images and not assets.videos:
        problems.append(f"No images or videos found in {rpath(cfg['backgrounds_dir'])}")
    if not assets.music:
        problems.append(f"No music files found in {rpath(cfg['music_dir'])}")
    if not rpath(cfg["look_file"]).exists():
        problems.append(f"Look file not found: {rpath(cfg['look_file'])}")
    if cfg["tts"]["engine"].lower() == "piper":
        model = rpath(cfg["tts"]["piper_model"])
        if not model.exists():
            problems.append(f"Piper voice model not found: {model}  (see README step 3)")
    return problems


def main() -> int:
    global LOG_FH
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Build the Bible-verse Shorts batch.")
    ap.add_argument("--config", default="config.json", help="settings file (default: config.json)")
    ap.add_argument("--batch", help="batch markdown file (default from config)")
    ap.add_argument("--days", help="only build these days, e.g. 1,3,5")
    ap.add_argument("--seed", type=int, help="random seed (repeatable choices)")
    ap.add_argument("--keep-temp", action="store_true", help="keep the work folder")
    args = ap.parse_args()

    cfg = dict(DEFAULTS)
    cfg_path = rpath(args.config)
    if cfg_path.exists():
        cfg = deep_merge(DEFAULTS, json.loads(cfg_path.read_text(encoding="utf-8")))
    if args.seed is not None:
        random.seed(args.seed)

    out_dir = rpath(cfg["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    LOG_FH = open(out_dir / "run_log.txt", "a", encoding="utf-8")
    log(f"\n=== Run started {time.strftime('%Y-%m-%d %H:%M:%S')} ===")

    batch_path = rpath(args.batch or cfg["batch_file"])
    assets = Assets(cfg)
    problems = preflight(cfg, assets, batch_path)
    if problems:
        log("Cannot start:")
        for p in problems:
            log("  - " + p)
        return 2

    days = parse_batch(batch_path.read_text(encoding="utf-8-sig"))
    if args.days:
        wanted = {int(x) for x in args.days.split(",")}
        days = [d for d in days if d["num"] in wanted]
    look_data = load_look(cfg)

    log(f"Batch: {batch_path.name} - {len(days)} video(s). Backgrounds: {len(assets.images)} image(s), "
        f"{len(assets.videos)} clip(s). Music: {len(assets.music)} track(s).")

    results, t0 = [], time.time()
    for n, day in enumerate(days, 1):
        log(f"\n[{n}/{len(days)}] Day {day['num']} - {day['heading']}")
        t1 = time.time()
        try:
            out = make_video(day, cfg, look_data, assets, args.keep_temp)
            log(f"  done -> {out.name}  ({time.time() - t1:.0f}s)")
            results.append((day["num"], True, out.name))
        except Exception as exc:                                   # keep going with the next day
            log(f"  FAILED: {exc}")
            LOG_FH.write(traceback.format_exc() + "\n")
            results.append((day["num"], False, str(exc).splitlines()[0]))

    ok = sum(1 for r in results if r[1])
    log(f"\n=== Finished: {ok}/{len(results)} succeeded in {(time.time() - t0) / 60:.1f} min ===")
    for num, good, info in results:
        log(f"  Day {num}: {'OK    ' if good else 'FAILED'} {info}")
    log(f"Output folder: {out_dir}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
