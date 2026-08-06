#!/usr/bin/env python3
"""
hinglish_poc.py - evaluate edge-tts for Hinglish narration.

    pip install edge-tts
    python hinglish_poc.py

Answers three questions the pipeline needs settled before any Hindi video:

  1. Which voice handles code-switching without mangling the transitions?
  2. Devanagari or romanised Latin - which input form sounds better?
  3. What is the real words-per-minute for Hinglish? (narrate.py assumes 165,
     which is an English number and almost certainly wrong here.)

Also proves out word-level timings, which edge-tts emits for free via
WordBoundary events - no WhisperX needed, and it works for Hindi where the
English alignment model would fail outright.

Outputs to ./hinglish_poc/ with an index.html for A/B listening.
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import edge_tts

OUT = Path("hinglish_poc")

# Same sentence three ways. Mixed is what brain-rot narration actually looks
# like: an English technical noun dropped into a Hindi sentence.
SAMPLES = {
    "devanagari": (
        "साल उन्नीस सौ उनहत्तर था। तीन आदमी एक धातु के कैप्सूल में बैठे थे। "
        "कैप्सूल चौबीस हज़ार मील प्रति घंटे की रफ़्तार से समुद्र से टकराया। "
        "हीट शील्ड जलकर एक तिहाई रह गई। तीन मिनट तक रेडियो संपर्क टूट गया।"
    ),
    "romanised": (
        "Saal unnees sau unhattar tha. Teen aadmi ek dhaatu ke capsule mein baithe the. "
        "Capsule chaubees hazaar mile prati ghante ki raftaar se samundar se takraaya. "
        "Heat shield jalkar ek tihaai reh gayi. Teen minute tak radio contact toot gaya."
    ),
    "mixed": (
        "साल 1969 था। तीन astronauts एक metal capsule में बैठे थे। "
        "Capsule 24,000 miles per hour की speed से ocean में टकराया। "
        "Heat shield जलकर एक तिहाई रह गई। तीन minute तक radio contact टूट गया।"
    ),
}

VOICES = [
    "hi-IN-MadhurNeural",     # Hindi male
    "hi-IN-SwaraNeural",      # Hindi female
    "en-IN-PrabhatNeural",    # Indian English male
    "en-IN-NeerjaNeural",     # Indian English female
]

RATE = "+8%"   # brain-rot narration usually runs slightly hot


async def list_indian_voices():
    voices = await edge_tts.list_voices()
    rows = [v for v in voices if v["Locale"] in ("hi-IN", "en-IN")]
    print(f"\n{len(rows)} Indian voices available:\n")
    for v in sorted(rows, key=lambda x: x["ShortName"]):
        pers = v.get("VoicePersonalities") or []
        print(f"  {v['ShortName']:<28} {v['Gender']:<7} {', '.join(pers)}")
    return [v["ShortName"] for v in rows]


async def synth(text, voice, stem):
    """Synthesize and capture WordBoundary events in one pass.

    This is the useful part: edge-tts streams word timings alongside the audio
    at no extra cost. Offsets are in 100-nanosecond ticks."""
    mp3 = OUT / f"{stem}.mp3"
    comm = edge_tts.Communicate(text, voice, rate=RATE)
    words = []

    with open(mp3, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                words.append({
                    "text": chunk["text"],
                    "start": chunk["offset"] / 1e7,
                    "end": (chunk["offset"] + chunk["duration"]) / 1e7,
                })

    (OUT / f"{stem}.words.json").write_text(
        json.dumps(words, ensure_ascii=False, indent=1))
    return mp3, words


def duration(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, check=True)
        return float(r.stdout.strip())
    except Exception:
        return 0.0


async def main():
    OUT.mkdir(exist_ok=True)

    try:
        available = await list_indian_voices()
    except Exception as e:
        sys.exit(f"could not reach the edge-tts service: {e}")

    missing = [v for v in VOICES if v not in available]
    if missing:
        print(f"\nnote: not currently offered -> {', '.join(missing)}")

    results = []
    for voice in [v for v in VOICES if v in available]:
        for form, text in SAMPLES.items():
            stem = f"{voice}__{form}"
            print(f"  synthesizing {stem} ...")
            try:
                mp3, words = await synth(text, voice, stem)
            except Exception as e:
                print(f"    failed: {e}")
                continue

            secs = duration(mp3) or (words[-1]["end"] if words else 0)
            n = len(text.split())
            results.append({
                "voice": voice, "form": form, "file": mp3.name,
                "words": n, "seconds": round(secs, 2),
                "wpm": round(n / (secs / 60), 1) if secs else 0,
                "timings": len(words),
            })

    if not results:
        sys.exit("nothing synthesized")

    (OUT / "results.json").write_text(json.dumps(results, indent=2))

    print(f"\n{'voice':<24}{'form':<13}{'words':>6}{'secs':>8}{'wpm':>8}{'timings':>9}")
    print("-" * 68)
    for r in results:
        print(f"{r['voice']:<24}{r['form']:<13}{r['words']:>6}"
              f"{r['seconds']:>8.1f}{r['wpm']:>8.1f}{r['timings']:>9}")

    wpms = [r["wpm"] for r in results if r["wpm"]]
    avg = sum(wpms) / len(wpms)
    print(f"\nmean Hinglish rate at {RATE}: {avg:.0f} wpm")
    print(f"word budget for 60s  : {avg * 1.0:.0f} words   "
          f"(narrate.py currently assumes 165)")

    write_index(results, avg)
    print(f"\nopen {OUT/'index.html'} to compare")


def write_index(results, avg):
    rows = "".join(
        f"<tr><td>{r['voice']}</td><td>{r['form']}</td>"
        f"<td class=n>{r['seconds']:.1f}s</td><td class=n>{r['wpm']:.0f}</td>"
        f"<td><audio controls preload=none src='{r['file']}'></audio></td></tr>"
        for r in results)

    (OUT / "index.html").write_text(f"""<!doctype html>
<meta charset=utf-8><title>Hinglish TTS comparison</title>
<style>
 body{{font:15px/1.6 system-ui,sans-serif;max-width:1000px;margin:40px auto;padding:0 20px}}
 table{{border-collapse:collapse;width:100%}}
 td,th{{padding:9px 12px;border-bottom:1px solid #e3e3e3;text-align:left}}
 th{{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:#666}}
 .n{{text-align:right;font-variant-numeric:tabular-nums}}
 audio{{height:34px}}
 .note{{background:#f6f6f4;padding:16px 20px;border-radius:8px;margin:24px 0}}
</style>
<h1>Hinglish TTS comparison</h1>
<div class=note>
 <b>Mean rate: {avg:.0f} wpm</b> at {RATE}. Use this for the word budget in
 <code>narrate.py</code> instead of the English 165.
 <br><br>
 Listen for: does the voice break stride at each English word? Do numbers read
 correctly in context? Does the romanised form get pronounced as English
 spelling rather than as Hindi?
</div>
<table>
<tr><th>voice</th><th>input form</th><th>length</th><th>wpm</th><th></th></tr>
{rows}
</table>""", encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
