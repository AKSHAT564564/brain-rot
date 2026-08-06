#!/usr/bin/env python3
"""
assemble.py - script.json + images + narration.mp3 + gameplay folder -> final short.

    python assemble.py --script out/script.json \
                       --images out/images \
                       --audio  out/narration.mp3 \
                       --gameplay footage/ \
                       --out    final.mp4

Layout: 1080x1920, images on top (1080x960), gameplay on bottom (1080x960),
captions centred on the seam of the full frame.

The narration is the master clock. Every shot is rendered to the duration that
forced alignment measured, so the stacked video matches the voice with no drift.
"""

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

W, H = 1080, 1920
PANEL_H = H // 2          # 960 per panel
FPS = 25
SUPERSAMPLE = 2           # render stills at 2x before zoompan, else the zoom is soft
MUSIC_GAIN = 0.12
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}

# Resolved at startup (resolve_ffmpeg) to a binary that actually has libass.
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if p.returncode != 0:
        sys.exit(f"\nffmpeg failed:\n  {' '.join(str(c) for c in cmd[:14])} ...\n"
                 f"{p.stderr[-1500:]}")
    return p


def duration(path: Path) -> float:
    p = run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    return float(p.stdout.strip())


def norm(text: str) -> str:
    """Normalise for matching: lowercase, drop punctuation, collapse space."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower())).strip()


def _has_ass_filter(ffmpeg: str) -> bool:
    """True if this ffmpeg build has the libass 'ass' video filter."""
    try:
        p = subprocess.run([ffmpeg, "-hide_banner", "-filters"],
                           capture_output=True, text=True)
    except OSError:
        return False
    return re.search(r"^\s*\S+\s+ass\s+", p.stdout, re.M) is not None


def resolve_ffmpeg():
    """Pick an ffmpeg/ffprobe pair whose ffmpeg includes libass, and set the
    module globals used by every run() call.

    The stock Homebrew `ffmpeg` bottle is built WITHOUT libass, so the caption
    burn otherwise dies with an opaque 'No such filter: ass' only after all the
    shots have rendered. We fail fast here instead, and transparently prefer the
    keg-only `ffmpeg-full`. Override with FFMPEG_BIN=/path/to/ffmpeg."""
    global FFMPEG, FFPROBE
    candidates = [
        os.environ.get("FFMPEG_BIN"),
        shutil.which("ffmpeg"),
        "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",
        "/usr/local/opt/ffmpeg-full/bin/ffmpeg",
    ]
    seen = set()
    for ff in candidates:
        if not ff:
            continue
        resolved = ff if Path(ff).exists() else shutil.which(ff)
        if not resolved or resolved in seen:
            continue
        seen.add(resolved)
        if _has_ass_filter(resolved):
            FFMPEG = resolved
            probe = Path(resolved).with_name("ffprobe")   # match the build
            FFPROBE = str(probe) if probe.exists() else "ffprobe"
            return FFMPEG, FFPROBE
    sys.exit(
        "no ffmpeg with libass (the 'ass' subtitle filter) was found, so captions\n"
        "cannot be burned. On macOS:\n"
        "    brew install ffmpeg-full\n"
        "or point at a libass build:  FFMPEG_BIN=/path/to/ffmpeg python assemble.py ...")


# ---------------------------------------------------------------------------
# step 4 - alignment
# ---------------------------------------------------------------------------

@dataclass
class Word:
    text: str
    start: float
    end: float


def align_whisperx(audio: Path, narration: str, device="cpu"):
    """Forced alignment against known text. Far more reliable than transcribing
    and fuzzy-matching, because we already know every word that was spoken."""
    import whisperx

    model_a, meta = whisperx.load_align_model(language_code="en", device=device)
    segments = [{"text": narration, "start": 0.0, "end": duration(audio)}]
    out = whisperx.align(segments, model_a, meta, str(audio), device,
                         return_char_alignments=False)

    words = []
    for seg in out["segments"]:
        for w in seg.get("words", []):
            if w.get("start") is None:
                continue
            words.append(Word(w["word"].strip(), float(w["start"]), float(w["end"])))
    return words


def load_timings(path: Path):
    """Load precomputed word timings (edge-tts / narrate.py words.json).

    Returns a list of Word or None if the file is missing or empty, so callers
    can fall through to whisperx / proportional alignment. Shape per entry:
    {"text","start","end"} in seconds."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    words = [Word(w["text"].strip(), float(w["start"]), float(w["end"]))
             for w in raw if w.get("start") is not None]
    return words or None


def align_proportional(audio: Path, narration: str):
    """Fallback when whisperx is not installed: spread words evenly across the
    real audio length. Beat boundaries land within a few hundred ms, which is
    fine for image cuts but too loose for karaoke captions."""
    total = duration(audio)
    toks = narration.split()
    step = total / max(len(toks), 1)
    return [Word(t, i * step, (i + 1) * step) for i, t in enumerate(toks)]


def beat_spans(words, beats):
    """Walk the aligned word list and cut it at beat boundaries by word count.

    Because alignment ran on the exact narration string we supplied, the word
    sequence matches the concatenated beat texts one-for-one - so counting is
    enough and no fuzzy matching is needed."""
    spans, cursor = [], 0
    for b in beats:
        n = len(norm(b["text"]).split())
        chunk = words[cursor:cursor + n]
        if not chunk:
            spans.append((words[-1].end, words[-1].end))
        else:
            spans.append((chunk[0].start, chunk[-1].end))
        cursor += n
    # stretch the last beat to the true end so trailing silence is covered
    if spans and words:
        spans[-1] = (spans[-1][0], max(spans[-1][1], words[-1].end))
    return spans


# ---------------------------------------------------------------------------
# step 5 - motion (Ken Burns)
# ---------------------------------------------------------------------------

def zoompan_filter(motion: str, frames: int) -> str:
    """zoompan's `d` is in FRAMES, not seconds - the most common bug here.
    `on` is the output frame index, so on/frames ramps 0->1 over the shot."""
    n = max(frames, 2)
    p = f"on/{n}"
    zi, zo = 1.0, 1.14          # zoom range
    cx, cy = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"

    if motion == "slow pull out":
        z, x, y = f"{zo}-{zo - zi}*{p}", cx, cy
    elif motion == "drift left":
        z, x, y = f"{zo}", f"(iw-iw/zoom)*(1-{p})", cy
    elif motion == "drift right":
        z, x, y = f"{zo}", f"(iw-iw/zoom)*{p}", cy
    elif motion == "tilt up":
        z, x, y = f"{zo}", cx, f"(ih-ih/zoom)*(1-{p})"
    elif motion == "tilt down":
        z, x, y = f"{zo}", cx, f"(ih-ih/zoom)*{p}"
    else:                        # slow push in (default)
        z, x, y = f"{zi}+{zo - zi}*{p}", cx, cy

    sw, sh = W * SUPERSAMPLE, PANEL_H * SUPERSAMPLE
    return (
        f"scale={sw}:{sh}:force_original_aspect_ratio=increase,"
        f"crop={sw}:{sh},"
        f"zoompan=z='{z}':x='{x}':y='{y}':d={n}:s={W}x{PANEL_H}:fps={FPS},"
        f"setsar=1"
    )


def render_shot(image: Path, motion: str, secs: float, out: Path):
    frames = max(int(round(secs * FPS)), 2)
    run([FFMPEG, "-y", "-loglevel", "error", "-i", str(image),
         "-vf", zoompan_filter(motion, frames),
         "-frames:v", str(frames), "-c:v", "libx264", "-crf", "18",
         "-pix_fmt", "yuv420p", "-r", str(FPS), str(out)])
    return frames


# ---------------------------------------------------------------------------
# gameplay panel
# ---------------------------------------------------------------------------

def build_gameplay(folder: Path, total: float, out: Path, seed=None, clip=None):
    """Random file + random in-point, so repeat uploads don't reuse footage.
    Pass `clip` (a filename in `folder`) to force a specific source instead."""
    clips = [p for p in sorted(folder.iterdir()) if p.suffix.lower() in VIDEO_EXTS]
    if not clips:
        sys.exit(f"no video files found in {folder}")

    rng = random.Random(seed)
    if clip:
        src = folder / clip
        if not src.exists():
            sys.exit(f"gameplay clip not found: {src}")
    else:
        src = rng.choice(clips)
    src_len = duration(src)
    offset = rng.uniform(0, max(src_len - total, 0)) if src_len > total + 1 else 0

    # -stream_loop covers the case where the source is shorter than the narration
    run([FFMPEG, "-y", "-loglevel", "error",
         "-stream_loop", "-1", "-ss", f"{offset:.2f}", "-i", str(src),
         "-t", f"{total:.3f}", "-an",
         "-vf", (f"scale={W}:{PANEL_H}:force_original_aspect_ratio=increase,"
                 f"crop={W}:{PANEL_H},fps={FPS},setsar=1"),
         "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", str(out)])
    return src.name, offset


# ---------------------------------------------------------------------------
# captions
# ---------------------------------------------------------------------------

# DejaVu Sans has no Devanagari glyphs, so Hinglish captions render as tofu
# boxes with it. Kohinoor Devanagari (a macOS system font) covers Devanagari and
# Latin, so it handles mixed-script Hinglish in one style.
LATIN_FONT = "DejaVu Sans"
DEVANAGARI_FONT = "Kohinoor Devanagari"


def has_devanagari(words) -> bool:
    return any("ऀ" <= c <= "ॿ" for w in words for c in w.text)


def pick_font(words, override: str = None) -> str:
    if override:
        return override
    return DEVANAGARI_FONT if has_devanagari(words) else LATIN_FONT


def ass_head(font: str) -> str:
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Base,{font},66,&H00FFFFFF,&H00000000,&H80000000,-1,0,1,5,2,5,60,60,0,1
Style: Hot,{font},66,&H0000E5FF,&H00000000,&H80000000,-1,0,1,5,2,5,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def ts(t: float) -> str:
    t = max(t, 0)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def write_ass(words, path: Path, font: str = LATIN_FONT, per_line=3):
    """Karaoke-style: one event per word, with the active word highlighted and
    its neighbours shown dimmer for context."""
    lines = []
    for i in range(0, len(words), per_line):
        lines.append(words[i:i + per_line])

    events = []
    for group in lines:
        for idx, w in enumerate(group):
            parts = []
            for j, g in enumerate(group):
                txt = g.text.upper()
                if j == idx:
                    parts.append(r"{\c&H0000E5FF&\fscx108\fscy108}" + txt + r"{\r}")
                else:
                    parts.append(r"{\alpha&H60&}" + txt + r"{\r}")
            events.append(
                f"Dialogue: 0,{ts(w.start)},{ts(w.end)},Base,,0,0,0,,"
                + " ".join(parts)
            )
    path.write_text(ass_head(font) + "\n".join(events) + "\n")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", required=True)
    ap.add_argument("--images", required=True)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--gameplay", required=True)
    ap.add_argument("--clip", default=None,
                    help="force a specific gameplay file in --gameplay (else random)")
    ap.add_argument("--music", default=None)
    ap.add_argument("--font", default=None,
                    help="caption font override (else auto: Devanagari-aware)")
    ap.add_argument("--out", default="final.mp4")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--timings", default=None,
                    help="precomputed word timings (words.json); if non-empty, "
                         "used instead of whisperx")
    ap.add_argument("--no-align", action="store_true",
                    help="skip whisperx, spread words proportionally")
    ap.add_argument("--keep", action="store_true", help="keep the temp work dir")
    args = ap.parse_args()

    spec = json.loads(Path(args.script).read_text())
    beats = spec["beats"]
    narration = spec["narration"]
    audio = Path(args.audio)
    imgdir = Path(args.images)

    # --- preflight: fail fast before rendering 15 shots -------------------
    resolve_ffmpeg()
    print(f"ffmpeg: {FFMPEG}", file=sys.stderr)

    # Resolve every visual to an image file on disk. A missing image (e.g. one
    # that failed to generate) must not kill the whole render - substitute the
    # nearest available image instead: search forward in shot order first (keeps
    # the story moving), then backward. Each visual gets a "_img" Path to use.
    flat = [v for b in beats for v in b["visuals"]]

    def img_ok(v):
        nm = v.get("image")
        return bool(nm) and (imgdir / nm).exists()

    if not any(img_ok(v) for v in flat):
        sys.exit(f"no images found in {imgdir} - run the Images stage first")

    def nearest_available(idx):
        for j in list(range(idx, len(flat))) + list(range(idx - 1, -1, -1)):
            if img_ok(flat[j]):
                return flat[j]["image"]
        return None

    subs, fi = [], 0
    for b in beats:
        for i, v in enumerate(b["visuals"]):
            if img_ok(v):
                v["_img"] = imgdir / v["image"]
            else:
                rep = nearest_available(fi)
                v["_img"] = imgdir / rep
                subs.append(f"beat {b['id']} visual {i}: "
                            f"{v.get('image') or 'no image key'} missing -> {rep}")
            fi += 1
    if subs:
        print("WARNING: substituting for missing images (render continues):\n  "
              + "\n  ".join(subs), file=sys.stderr)

    # captions are timed by counting narration words off against beat texts, so
    # the two must be the same words in the same order or the timing drifts.
    joined = " ".join(b["text"] for b in beats)
    if norm(joined) != norm(narration):
        print(f"WARNING: narration != beats joined "
              f"({len(norm(narration).split())} vs {len(norm(joined).split())} "
              f"words) - caption timing may drift.", file=sys.stderr)

    work = Path(tempfile.mkdtemp(prefix="assemble_"))
    print(f"work dir: {work}", file=sys.stderr)

    # --- step 4 -----------------------------------------------------------
    # Selection order: precomputed --timings (if non-empty) -> whisperx -> proportional.
    words = None
    if args.timings:
        words = load_timings(Path(args.timings))
        if words:
            print(f"alignment: precomputed timings, {len(words)} words", file=sys.stderr)
        else:
            print("alignment: --timings empty, falling back", file=sys.stderr)

    if words is None:
        if args.no_align:
            words = align_proportional(audio, narration)
            print("alignment: proportional (captions will be approximate)", file=sys.stderr)
        else:
            try:
                words = align_whisperx(audio, narration)
                print(f"alignment: whisperx, {len(words)} words", file=sys.stderr)
            except ImportError:
                words = align_proportional(audio, narration)
                print("alignment: whisperx not installed, falling back to proportional",
                      file=sys.stderr)

    spans = beat_spans(words, beats)
    total = duration(audio)

    # --- step 5 -----------------------------------------------------------
    shot_files = []
    for beat, (b_start, b_end) in zip(beats, spans):
        vis = beat["visuals"]
        b_len = max(b_end - b_start, 0.4)
        # split the beat's measured time across its shots by their planned weights
        weights = [max(float(v.get("seconds", 1)), 0.1) for v in vis]
        wsum = sum(weights)

        for v, wt in zip(vis, weights):
            img = v["_img"]
            secs = b_len * wt / wsum
            out = work / f"shot_{len(shot_files):03d}.mp4"
            render_shot(img, v.get("motion", "slow push in"), secs, out)
            shot_files.append(out)

    print(f"rendered {len(shot_files)} shots over {total:.1f}s", file=sys.stderr)

    concat = work / "shots.txt"
    concat.write_text("".join(f"file '{p}'\n" for p in shot_files))
    top = work / "top.mp4"
    run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(concat), "-c", "copy", str(top)])

    # --- gameplay ---------------------------------------------------------
    bottom = work / "bottom.mp4"
    src_name, off = build_gameplay(Path(args.gameplay), total, bottom,
                                   args.seed, args.clip)
    print(f"gameplay: {src_name} @ {off:.1f}s", file=sys.stderr)

    # --- captions ---------------------------------------------------------
    ass = work / "captions.ass"
    font = pick_font(words, args.font)
    print(f"caption font: {font}", file=sys.stderr)
    write_ass(words, ass, font)

    # --- step 6: bind -----------------------------------------------------
    inputs = ["-i", str(top), "-i", str(bottom), "-i", str(audio)]
    if args.music:
        inputs += ["-i", str(args.music)]
        amix = ("[2:a]volume=1.0[nar];"
                f"[3:a]volume={MUSIC_GAIN},afade=t=out:st={max(total-1.5,0):.2f}:d=1.5[mus];"
                "[nar][mus]amix=inputs=2:duration=first:dropout_transition=0[a]")
    else:
        amix = "[2:a]volume=1.0[a]"

    # libass needs the path escaped inside the filter string
    ass_esc = str(ass).replace("\\", "/").replace(":", r"\:")
    fc = (f"[0:v][1:v]vstack=inputs=2[stack];"
          f"[stack]ass='{ass_esc}'[v];{amix}")

    run([FFMPEG, "-y", "-loglevel", "error", *inputs,
         "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
         "-c:v", "libx264", "-crf", "20", "-preset", "medium",
         "-pix_fmt", "yuv420p", "-r", str(FPS),
         "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
         "-shortest", args.out])

    out = Path(args.out)
    print(f"\n{out}  {duration(out):.1f}s  {out.stat().st_size/1e6:.1f} MB")

    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
