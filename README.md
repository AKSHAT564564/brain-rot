# Brain-rot video factory

Turns a story/article into a finished 1080×1920 vertical short: AI images on top,
muted gameplay on the bottom, karaoke captions on the seam, over TTS narration.

Run the whole thing from one local UI, or drive each stage from the CLI.

```
story/article ─▶ script.json ─▶ narration.mp3 ─▶ images/*.png ─▶ final.mp4
                 (plan.py)       (narrate.py)     (generate_      (assemble.py)
                 Claude/Gemini    edge-tts         images.py)      ffmpeg
                 EN or Hinglish                    Cloudflare FLUX  + music + gameplay
                      │
                      └─▶ metadata.json  (metadata.py — title / description / hashtags / tags)
```

## Setup

    pip install edge-tts whisperx          # whisperx optional (English caption sync)
    brew install ffmpeg-full               # ffmpeg WITH libass (needed for captions)
    cp .env.example .env                   # then fill in the keys

Script generation has three providers (pick per project in the UI):
- **Claude Code (subscription)** — default; runs the local `claude` CLI headless,
  so it uses your Claude Code login and needs **no API key**.
- **Claude API** — needs `ANTHROPIC_API_KEY`.
- **Gemini API** — needs `GEMINI_API_KEY`.

`.env` keys: `ANTHROPIC_API_KEY` and/or `GEMINI_API_KEY` (only for the API
providers), `CF_ACCOUNT_ID` + `CF_API_TOKEN` (images).

## The UI (recommended)

    python studio.py            # http://localhost:7861

- **New project** (left rail) — a title creates `projects/<slug>/`; all of that
  project's assets live there and never mix with another project's.
- Work down the stage cards: **Plan → Narrate → Images → Metadata → Assemble**.
  Each has its controls, a Run button, a live log, and an inline preview (audio /
  image grid / final video). After Plan you can view/edit `script.json` (and after
  Metadata, edit the title/description/tags) before spending image quota.
- **Language** (Plan card): English or **Hinglish** — Hinglish writes mixed
  Devanagari+English narration (~120 wpm) and captions auto-switch to a
  Devanagari font; Hinglish projects default the voice to a Hindi voice.
- **Voice**: dropdown of edge-tts voices (English first, then Hindi) with a
  **▶ Preview** button to audition before committing. Rate / pitch / volume faders.
- **Assemble card**: pick **Gameplay** (Random or a specific clip from `footage/`)
  and **Music** (None / Random / a track from `music/`, ducked under narration).

## Per-project layout

    projects/<slug>/
      meta.json              title, seconds, language, voice, music/clip, per-stage status
      context.txt            the pasted story/article
      script.json            plan output (image keys injected)
      narration.mp3          narration
      narration.words.json   word timings from edge-tts
      images/img_001.png …   one image per visual
      metadata.json          title / description / hashtags / tags
      logs/<stage>.log       captured stage output
      final.mp4              the finished short

## Media libraries

- `footage/` — gameplay clips. Add as many as you like; assemble picks one at
  random (or force one with the UI Gameplay picker / `--clip`).
- `music/` — royalty-free background tracks (see `music/README.md` for safe
  sources). Ducked under narration. **Not** for copyrighted "trending" audio.

## CLI (same stages, no UI)

    python plan.py     --context context.txt --seconds 60 [--language english|hinglish] \
                       [--guidance "tone/POV"] [--provider claude-cli|claude|gemini] --out out/script.json
    python narrate.py  --script out/script.json --voice hi-IN-MadhurNeural \
                       --out-mp3 out/narration.mp3 --out-words out/narration.words.json
    python generate_images.py --script out/script.json --out out/images --provider cloudflare
    python metadata.py --script out/script.json --out out/metadata.json
    python assemble.py --script out/script.json --images out/images --audio out/narration.mp3 \
                       --gameplay footage/ [--clip NAME] [--music music/track.mp3] \
                       --timings out/narration.words.json --out final.mp4

See `pipeline-README.md` for assemble.py internals and `ARCHITECTURE.md` for the
full design and known limitations.

## Captions & language

edge-tts emits word-level timings for **every** voice — English and Hindi/
Hinglish alike — as long as it's asked for them (`boundary="WordBoundary"`, which
`narrate.py` sets). `narrate.py` writes those to `narration.words.json`, and
`assemble.py --timings` uses them directly for word-synced karaoke captions. No
whisperx, no model download, and no Devanagari-alignment problem.

whisperx remains available as a fallback (`assemble.py` uses it only when no
timings are supplied). The alignment order is: `--timings` (if non-empty) →
whisperx → proportional.

**Hinglish captions:** `DejaVu Sans` can't render Devanagari, so `assemble.py`
detects Devanagari in the caption text and switches to **Kohinoor Devanagari** (a
macOS system font that covers Devanagari + Latin, so mixed Hinglish renders in one
style). Override with `--font "<name>"`.

## Customizing narration

The output *structure* is fixed by `story-to-shotlist-prompt.md` (sent to Claude
verbatim). To steer tone/POV/emphasis without changing structure, use the
**Narration guidance** field in the UI (or `--guidance` on `plan.py`).
