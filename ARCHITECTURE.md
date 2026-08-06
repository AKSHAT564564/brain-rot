# Brain-rot pipeline — build status & architecture

_Last updated: 2026-08-02. The legs are now wired: `plan.py`, `narrate.py`, and
`studio.py` (the orchestrator UI) exist, `plan.py` injects image keys, and
`assemble.py` consumes edge-tts timings. See `README.md` for how to run it; the
"Open questions / risks" and "TL;DR" sections below are current. The
stage-by-stage prose above them predates the wiring and is kept for background._

## What this project is

An automated **short-form vertical video factory**. You feed in raw source text
(a story, article, notes) and it produces a finished **1080×1920 MP4** in the
"brain-rot" format: AI-generated images on the top half, muted gameplay footage
on the bottom half, karaoke word-by-word captions on the seam, over a TTS
narration.

The project is mid-**pivot from English → Hinglish/Hindi narration**, which is
why there are now two narration-related experiments (`tts_studio.py`,
`hinglish_poc.py`) that aren't yet wired into the main flow.

---

## The pipeline, end to end

```
  raw story text
        │
        ▼
 ┌──────────────────────┐  story-to-shotlist-prompt.md   (LLM prompt, run by hand)
 │ 1. PLAN              │ ─────────────────────────────►  out/script.json
 └──────────────────────┘   narration + shot list + prompts
        │
        ├───────────────────────────────┐
        ▼                                ▼
 ┌──────────────────────┐        ┌──────────────────────┐
 │ 2. IMAGES            │        │ 3. NARRATION         │
 │ generate_images.py   │        │ (no production script │
 │ script.json → PNGs   │        │  yet — narrate.py     │
 └──────────────────────┘        │  is MISSING)          │
        │                        └──────────────────────┘
        │  out/images/*.png              │  narration.mp3 (+ word timings?)
        │                                │
        └───────────────┬────────────────┘
                        ▼
             ┌──────────────────────┐   + footage/  (gameplay)
             │ 4–6. ASSEMBLE        │   + music.mp3 (optional)
             │ assemble.py          │
             └──────────────────────┘
                        │
                        ▼
                    final.mp4  ✅ working
```

### Stage status at a glance

| Stage | Artifact in → out | Implemented by | Status |
|---|---|---|---|
| 1. Plan | story text → `script.json` | `plan.py` (Claude API) + `story-to-shotlist-prompt.md` | ✅ wired; injects `image` keys |
| 2. Images | `script.json` → `images/*.png` | `generate_images.py` (Cloudflare) | ✅ wired |
| 3. Narration | narration text → `narration.mp3` (+timings) | `narrate.py` (edge-tts) | ✅ wired; emits word timings |
| 4–6. Assemble | all of the above → `final.mp4` | `assemble.py` | ✅ working; consumes `--timings` |

**`studio.py`** is the orchestrator UI tying all stages together, one project at
a time, under `projects/<slug>/`. `tts_studio.py` (prosody bench) and
`hinglish_poc.py` (evaluation) remain as R&D side tools.

---

## Component-by-component

### `story-to-shotlist-prompt.md` — Stage 1 (the planning brain)
- **What:** an LLM system prompt. Input = raw context + target duration; output =
  one strict `script.json`.
- **Does the arithmetic first:** word budget = `D × 2.7` (assumes **165 wpm**,
  an English number), beat count (1 per 8–10s), visual count = `D ÷ 4`.
- Locks a `style_block` (one look sentence reused verbatim) and per-character
  physical descriptions (repeated verbatim) for cross-shot consistency.
- **Gap:** the output schema emits each visual's `prompt`/`shot`/`motion`/
  `seconds` but **no `image` filename** — which both `generate_images.py` and
  `assemble.py` require (see Contracts + Gaps below).

### `generate_images.py` — Stage 2 (image generation)
- **What:** `script.json` → the PNGs `assemble.py` expects.
- **Providers:** `cloudflare` (FLUX.1 Schnell, free, default), `fal` (paid,
  faster), `pollinations` (no key, smoke-test grade). All **FLUX / watermark-free**
  — unlike the current OpenArt images which carry a watermark.
- **Consistency:** appends `style_block` + matching character descriptions
  verbatim; one video-level seed (derived from title) with a deterministic
  per-image offset, so runs reproduce and images stay distinct.
- **Dedup:** `collect()` produces one job per distinct `image` filename, so
  visuals that share a still (different Ken Burns moves) are generated once.
- **Requires:** every visual already has an `image` key (`collect()` hard-exits
  if missing). Also has a `--prompt` single-image mode and `--dry-run`.
- **Status:** standalone-runnable; **never run in this project yet** (the 7 images
  in `out/images/` are hand-placed OpenArt exports).

### `assemble.py` — Stages 4–6 (assembly) ✅
- **What:** `script.json` + images + `narration.mp3` + gameplay → `final.mp4`.
- Narration is the master clock: **whisperx forced alignment** on the known
  narration text → beat spans by word count → each shot rendered to its exact
  slice → Ken Burns (`zoompan`) → vstack with gameplay → burn `.ass` karaoke
  captions → mux (optional ducked music).
- **Recently hardened:** auto-discovers a libass ffmpeg (prefers keg-only
  `ffmpeg-full`, `FFMPEG_BIN` override) and fails fast with instructions if none;
  preflight validates every visual's `image` exists and warns if narration ≠
  beats-joined. See `pipeline-README.md`.
- **Proven:** produced `final.mp4` (53.6s, English narration, whisperx-aligned).
- **Note for the pivot:** alignment currently assumes the **English** whisperx
  model. That model will not align Devanagari, and edge-tts word timings (if we
  had them) are not consumed here yet.

### `tts_studio.py` — Stage 3 R&D (prosody tuner)
- **What:** local web UI (`localhost:7860`) over **edge-tts**. Paste text, pick a
  voice (Indian locales surfaced first), drag rate/pitch/volume faders, Render;
  shows a 60s word-budget meter, measured wpm, and word timings.
- Defaults to Hindi (`hi-IN-MadhurNeural`) with a Hinglish sample.
- **Role:** a bench for choosing voice + prosody. **Not** a pipeline step; it
  produces no persisted artifact for the pipeline.
- Requests timings via `boundary="WordBoundary"` — but see the timings risk below.

### `hinglish_poc.py` — Stage 3 R&D (evaluation)
- **What:** answers three questions before committing to Hindi: best voice for
  code-switching, Devanagari vs romanised vs mixed input, and the real Hinglish
  wpm (vs the English 165 assumption).
- Synthesizes the same sentence 3 ways × 4 voices → `hinglish_poc/` with mp3s,
  per-clip `*.words.json`, `results.json`, and an A/B `index.html`.
- **Measured results (already in `hinglish_poc/results.json`):**
  - Hindi voices (Madhur/Swara) on Hinglish run **~116–129 wpm** — far below the
    English **165**. Word budget for 60s Hinglish ≈ **~120 words**, not 162.
  - `en-IN` voices read the "mixed" form fast (~175 wpm) but that's misleading —
    they can't pronounce Devanagari and skip it; `en-IN` + Devanagari **failed
    outright** (no output).
  - **`timings: 0` for every clip** — WordBoundary came back empty (see risk #1).
- References a **`narrate.py` that does not exist** — the intended production
  narration script.

---

## Data contracts

### `script.json` (current shape)
```jsonc
{
  "title", "target_seconds",
  "plan": { "word_budget", "beat_count", "visual_count" },
  "style_block": "one look sentence, reused verbatim",
  "characters": { "role": "fixed physical description" },
  "beats": [
    { "id", "text",
      "visuals": [
        { "image": "file.png",   // ← required by generate_images.py + assemble.py
          "prompt": "...",        // ← used by generate_images.py
          "shot", "motion", "seconds" } ] } ],
  "narration": "all beat texts joined, verbatim",
  "word_count"
}
```
- `narration` **must** equal the beat texts joined (assemble.py counts words off
  it against beat texts; a mismatch drifts caption timing — now warned).
- `image` reuse across visuals is intentional (7 images → 15 visuals here).
- **No `language` field** — planner/output is English-oriented today.

### edge-tts word timings (`*.words.json`)
`[{ "text", "start", "end" }, …]`, seconds. Offsets come from WordBoundary events
as 100-ns ticks (÷1e7). This shape is close to what assemble.py's aligner
produces internally — **if** the events are emitted.

---

## Missing legs / wiring TODO

1. **`narrate.py` (Stage 3 production script)** — doesn't exist. Needs to turn
   `script.json.narration` → `out/narration.mp3` **and** word timings, using the
   voice/prosody chosen via `tts_studio.py`.
2. **`image`-key assignment (Stage 1→2 contract)** — the shotlist prompt emits
   no `image` filenames, but Stage 2 and 4 require them (with deliberate reuse).
   Either update `story-to-shotlist-prompt.md` to assign them, or add a post-step.
   (Currently done by hand.)
3. **assemble.py should consume precomputed timings** — if narration comes from
   edge-tts (or any source with timings), pass them in and skip whisperx entirely
   (also sidesteps the whisperx model-download hang). Needs a `--timings` path or
   equivalent; not implemented.
4. **Hinglish word budget** — planner assumes 165 wpm; measured Hinglish is
   ~120. Stage 1 arithmetic (`D × 2.7`) needs a language-aware constant.
5. **No orchestrator** — no single command runs plan → images → narrate →
   assemble.

---

## Open questions / risks

1. **✅ RESOLVED — edge-tts word timings work for both languages.** The earlier
   "zero WordBoundary events" finding was a bug in `hinglish_poc.py`: it called
   `edge_tts.Communicate(...)` **without** `boundary="WordBoundary"`. With the
   parameter (which `narrate.py` and `tts_studio.py` both set), edge-tts emits
   word timings for English (157 for a 161-word script) **and** Hindi (12 for a
   12-word Devanagari sentence; mixed Hinglish too). `narrate.py` writes them to
   `narration.words.json` and `assemble.py --timings` consumes them directly, so
   captions are word-synced in either language with **no whisperx** and no
   Devanagari-alignment problem. whisperx stays only as a fallback.
2. **Image-cut drift on token mismatch** — `beat_spans` counts narration words
   against the timing list; edge-tts tokenisation can differ slightly (157 vs
   161), so image *transitions* may drift ~1s over a clip. Captions are unaffected
   (they use the timings directly). Acceptable; image cuts are forgiving.
3. **Watermark** — the original 7 images were watermarked OpenArt exports;
   `generate_images.py` (Cloudflare FLUX) produces watermark-free images and is
   now the wired Stage 2 (needs `CF_ACCOUNT_ID` + `CF_API_TOKEN`).

---

## TL;DR — now wired

- **Full pipeline runs from `studio.py`** (or per-stage CLI):
  `plan.py` (Claude) → `narrate.py` (edge-tts) → `generate_images.py` (Cloudflare)
  → `assemble.py` → `final.mp4`, with per-project asset segregation under
  `projects/<slug>/`.
- Stage 1→2 contract closed: `plan.py` injects sequential `image` keys.
- Alignment: edge-tts `--timings` is primary (both languages); whisperx is the
  fallback. Verified end-to-end (video duration == audio duration to the ms).
