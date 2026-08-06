#!/usr/bin/env python3
"""
narrate.py - Stage 3: script.json narration -> narration.mp3 (+ word timings).

    python narrate.py --script out/script.json \
                      --voice en-US-AriaNeural \
                      --out-mp3 out/narration.mp3 \
                      --out-words out/narration.words.json

The production counterpart to tts_studio.py (the prosody bench). It synthesizes
the full narration with edge-tts and captures WordBoundary events in the same
pass. Those timings are written out for assemble.py's --timings path; note that
edge-tts frequently emits zero boundaries, in which case the words file is an
empty list and assemble.py falls back to whisperx / proportional alignment.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import edge_tts


async def synth(text, voice, rate, pitch, volume):
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", required=True, help="script.json (reads .narration)")
    ap.add_argument("--voice", default="en-US-AriaNeural")
    ap.add_argument("--rate", default="+0%")
    ap.add_argument("--pitch", default="+0Hz")
    ap.add_argument("--volume", default="+0%")
    ap.add_argument("--out-mp3", default="out/narration.mp3")
    ap.add_argument("--out-words", default="out/narration.words.json")
    args = ap.parse_args()

    spec = json.loads(Path(args.script).read_text())
    text = (spec.get("narration") or "").strip()
    if not text:
        sys.exit(f"{args.script} has no 'narration' text")

    print(f"narrating: voice={args.voice} rate={args.rate} "
          f"pitch={args.pitch} volume={args.volume}", file=sys.stderr)
    try:
        mp3, words = asyncio.run(
            synth(text, args.voice, args.rate, args.pitch, args.volume))
    except Exception as e:
        sys.exit(f"edge-tts failed: {e}")

    if not mp3:
        sys.exit("edge-tts returned no audio")

    mp3_path = Path(args.out_mp3)
    mp3_path.parent.mkdir(parents=True, exist_ok=True)
    mp3_path.write_bytes(mp3)

    words_path = Path(args.out_words)
    words_path.parent.mkdir(parents=True, exist_ok=True)
    words_path.write_text(json.dumps(words, ensure_ascii=False, indent=1))

    kb = len(mp3) / 1024
    note = "" if words else "  (no word boundaries - assemble.py will align)"
    print(f"wrote {mp3_path}  ({kb:.0f} KB), {len(words)} word timings{note}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
