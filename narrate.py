#!/usr/bin/env python3
"""
narrate.py - Stage 3: script.json narration -> narration.mp3 (+ word timings).

    python narrate.py --script out/script.json \
                      --voice en-US-AriaNeural \
                      --out-mp3 out/narration.mp3 \
                      --out-words out/narration.words.json

Two providers:

    edge          Microsoft edge-tts. Free, instant, cloud. WordBoundary events
                  captured in the same pass (often empty for hi-IN voices, in
                  which case assemble.py falls back to whisperx / proportional).

    indic-parler  AI4Bharat Indic-Parler-TTS, run locally (Apple Silicon MPS /
                  CPU). Purpose-built for Indian languages and code-mixed
                  Hinglish, so it sounds markedly more natural than edge-tts on
                  Devanagari-plus-English narration. Slower (~1-3 min per short
                  on an M-series Mac) and needs the ML deps installed:

                      pip install torch transformers soundfile \
                          git+https://github.com/huggingface/parler-tts.git

                  It emits no word timings, so assemble.py aligns downstream.
"""

import argparse
import asyncio
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path


# ------------------------------------------------------------------ edge-tts

async def _edge_synth(text, voice, rate, pitch, volume):
    import edge_tts
    comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch,
                                volume=volume, boundary="WordBoundary")
    audio, words = bytearray(), []
    async for c in comm.stream():
        if c["type"] == "audio":
            audio.extend(c["data"])
        elif c["type"] == "WordBoundary":
            words.append({
                "text": c["text"],
                "start": round(c["offset"] / 1e7, 3),
                "end": round((c["offset"] + c["duration"]) / 1e7, 3),
            })
    return bytes(audio), words


def synth_edge(text, args):
    mp3, words = asyncio.run(
        _edge_synth(text, args.voice, args.rate, args.pitch, args.volume))
    if not mp3:
        sys.exit("edge-tts returned no audio")
    Path(args.out_mp3).write_bytes(mp3)
    return words


# ------------------------------------------------------- Indic-Parler-TTS

# Named speakers keep the voice consistent across chunks; these are the
# recommended Hindi speakers from the AI4Bharat model card.
INDIC_SPEAKERS = {
    "divya": "Divya", "rani": "Rani",      # female
    "rohit": "Rohit", "aman": "Aman",      # male
    "female": "Divya", "male": "Rohit",    # aliases
}
DEFAULT_SPEAKER = "Divya"

# Map the story's tone (emitted by plan.py) to an emotion phrase the model
# understands. Substring match, first hit wins; falls back to a neutral
# storyteller delivery.
TONE_EMOTION = [
    ("eerie", "in a tense, eerie tone"),
    ("creepy", "in a tense, eerie tone"),
    ("tense", "in a tense, suspenseful tone"),
    ("suspense", "in a tense, suspenseful tone"),
    ("thrill", "in a tense, suspenseful tone"),
    ("caution", "in a serious, cautionary tone"),
    ("tragic", "in a somber, emotional tone"),
    ("sad", "in a somber, emotional tone"),
    ("melanch", "in a somber, emotional tone"),
    ("triumph", "in an uplifting, triumphant tone"),
    ("inspir", "in an uplifting, inspiring tone"),
    ("hopeful", "in a warm, hopeful tone"),
    ("absurd", "in a playful, amused tone"),
    ("funny", "in a playful, amused tone"),
    ("humor", "in a playful, amused tone"),
    ("comic", "in a playful, amused tone"),
    ("warm", "in a warm, heartfelt tone"),
    ("tender", "in a warm, tender tone"),
    ("shock", "in a dramatic, urgent tone"),
    ("dramatic", "in a dramatic, expressive tone"),
]
DEFAULT_EMOTION = "in an expressive, dramatic storyteller's tone"


def build_description(spec, speaker):
    """Auto-compose the Indic-Parler style prompt from the chosen speaker and
    the story's tone, so the caller doesn't hand-write one each time."""
    name = INDIC_SPEAKERS.get((speaker or "").strip().lower(), DEFAULT_SPEAKER)
    tone = (spec.get("tone") or "").strip().lower()
    emotion = DEFAULT_EMOTION
    for key, phrase in TONE_EMOTION:
        if key in tone:
            emotion = phrase
            break
    return (f"{name} speaks {emotion} at a moderate pace, with a clear, "
            f"close-sounding, very high quality recording and no background noise.")


def _chunk(text, max_words=32):
    """Split narration into sentence-ish chunks.

    Parler synthesises a sentence or two cleanly but degrades on long passages,
    so we cut on sentence enders (English . ! ? and the Devanagari danda) and
    group them up to a word ceiling, then concatenate the audio."""
    sentences = re.split(r"(?<=[.!?।])\s+", text.strip())
    chunks, cur, n = [], [], 0
    for s in sentences:
        w = len(s.split())
        if cur and n + w > max_words:
            chunks.append(" ".join(cur))
            cur, n = [], 0
        cur.append(s)
        n += w
    if cur:
        chunks.append(" ".join(cur))
    return [c for c in chunks if c.strip()]


def synth_indic_parler(text, args):
    try:
        import numpy as np
        import soundfile as sf
        import torch
        from transformers import AutoTokenizer
        from parler_tts import ParlerTTSForConditionalGeneration
    except ImportError as e:
        sys.exit(
            f"indic-parler needs ML deps ({e.name} missing). Install with:\n"
            "  pip install torch transformers soundfile "
            "git+https://github.com/huggingface/parler-tts.git")

    # Prefer an NVIDIA GPU (Windows/Linux), then Apple Silicon (mac), else CPU.
    if torch.cuda.is_available():
        device = "cuda"
    elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        device = "mps"
    else:
        device = "cpu"
    model_id = "ai4bharat/indic-parler-tts"
    print(f"loading {model_id} on {device} (first run downloads ~2-3 GB)...",
          file=sys.stderr)

    model = ParlerTTSForConditionalGeneration.from_pretrained(model_id).to(device)
    tok = AutoTokenizer.from_pretrained(model_id)
    desc_tok = AutoTokenizer.from_pretrained(
        model.config.text_encoder._name_or_path)

    sr = model.config.sampling_rate
    desc = args.description  # already resolved in main() (auto or override)
    print(f"  style: {desc}", file=sys.stderr)
    d = desc_tok(desc, return_tensors="pt").to(device)

    chunks = _chunk(text)
    gap = np.zeros(int(sr * 0.25), dtype=np.float32)
    pieces = []
    for i, ch in enumerate(chunks, 1):
        print(f"  [{i}/{len(chunks)}] synth ({len(ch.split())} words)...",
              file=sys.stderr)
        p = tok(ch, return_tensors="pt").to(device)
        with torch.no_grad():
            gen = model.generate(
                input_ids=d.input_ids, attention_mask=d.attention_mask,
                prompt_input_ids=p.input_ids,
                prompt_attention_mask=p.attention_mask)
        audio = gen.cpu().numpy().squeeze().astype(np.float32)
        pieces.append(audio)
        if i < len(chunks):
            pieces.append(gap)

    full = np.concatenate(pieces) if pieces else np.zeros(1, dtype=np.float32)

    # soundfile writes wav; ffmpeg transcodes to the mp3 the pipeline expects.
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = tmp.name
    sf.write(wav_path, full, sr)
    subprocess.run(
        ["ffmpeg", "-y", "-i", wav_path, "-q:a", "2", args.out_mp3],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    Path(wav_path).unlink(missing_ok=True)
    return []  # no word timings; assemble.py aligns downstream


PROVIDERS = {"edge": synth_edge, "indic-parler": synth_indic_parler}


# -------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", required=True, help="script.json (reads .narration)")
    ap.add_argument("--provider", default="edge", choices=list(PROVIDERS))
    ap.add_argument("--voice", default="en-US-AriaNeural", help="edge-tts voice")
    ap.add_argument("--rate", default="+0%")
    ap.add_argument("--pitch", default="+0Hz")
    ap.add_argument("--volume", default="+0%")
    ap.add_argument("--speaker", default=DEFAULT_SPEAKER,
                    help="indic-parler voice: Divya/Rani (f), Rohit/Aman (m)")
    ap.add_argument("--description", default=None,
                    help="indic-parler style prompt (overrides the auto one)")
    ap.add_argument("--out-mp3", default="out/narration.mp3")
    ap.add_argument("--out-words", default="out/narration.words.json")
    args = ap.parse_args()

    spec = json.loads(Path(args.script).read_text())
    text = (spec.get("narration") or "").strip()
    if not text:
        sys.exit(f"{args.script} has no 'narration' text")

    Path(args.out_mp3).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out_words).parent.mkdir(parents=True, exist_ok=True)

    # Indic-Parler: auto-compose the style prompt from speaker + story tone
    # unless the caller passed an explicit --description override.
    if args.provider == "indic-parler" and not args.description:
        args.description = build_description(spec, args.speaker)

    if args.provider == "edge":
        print(f"narrating: edge voice={args.voice} rate={args.rate} "
              f"pitch={args.pitch} volume={args.volume}", file=sys.stderr)
    else:
        print(f"narrating: {args.provider}", file=sys.stderr)

    try:
        words = PROVIDERS[args.provider](text, args)
    except SystemExit:
        raise
    except Exception as e:
        sys.exit(f"{args.provider} failed: {e}")

    mp3_path = Path(args.out_mp3)
    if not mp3_path.exists() or mp3_path.stat().st_size == 0:
        sys.exit(f"{args.provider} produced no audio at {mp3_path}")

    Path(args.out_words).write_text(
        json.dumps(words, ensure_ascii=False, indent=1))

    kb = mp3_path.stat().st_size / 1024
    note = "" if words else "  (no word boundaries - assemble.py will align)"
    print(f"wrote {mp3_path}  ({kb:.0f} KB), {len(words)} word timings{note}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
