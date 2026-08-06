# Brain-rot — improvements plan

## Context

The core pipeline is wired and working end-to-end from `studio.py`
(plan → narrate → images → assemble → `final.mp4`), one project per title under
`projects/<slug>/`. `improvements.md` lists eight enhancements. This plan turns
them into an executable, phased build.

**Decisions (from user):**
- **Voice (#7):** stay on **edge-tts** (free) — curate the best voices, add a
  preview/compare step. No paid/local model.
- **Music (#5):** a **royalty-free library** folder (safe for monetized YouTube),
  not actual copyrighted trending audio.
- **YouTube (#3):** upload as **private with a scheduled `publishAt`** go-live.
- **Hinglish (#1):** narration in **mixed Devanagari + English** (natural
  brain-rot Hinglish); captions rendered with a Devanagari-capable font.

**Grounding facts (verified in the current code):**
- Captions use `DejaVu Sans` (`assemble.py` `ASS_HEAD`, lines ~267–268) → cannot
  render Devanagari. macOS has Devanagari fonts visible to ffmpeg's fontconfig
  (Kohinoor Devanagari, Mukta, Shobhika, Devanagari MT) → font swap, not a blocker.
- Word budget = `D × 2.7` (165 wpm, English) in `story-to-shotlist-prompt.md`;
  measured Hinglish ≈ 120 wpm → needs a language-aware constant (`D × 2.0`).
- `assemble.py` already random-picks gameplay from a folder and already supports
  `--music` with ducking + fade → both are selection/source problems, not new DSP.
- No upload/metadata code exists; `googleapiclient` / `google-auth-oauthlib` are
  **not installed** → YouTube needs new deps + one-time OAuth setup.
- Provider plumbing in `plan.py` (`call_claude_cli` / `call_claude` / `call_gemini`,
  default `claude-cli` on subscription) is reusable for any new LLM step.

---

## Phased roadmap

| Phase | Workstream | Requirement | External setup |
|---|---|---|---|
| **1** | A. Hinglish end-to-end | #1 | none |
| **1** | B. Metadata generator (title/description/hashtags) | #2, #4, #6 | none |
| **1** | C. Music library | #5 | source tracks |
| **1** | D. Multi-gameplay library | #8 | add clips |
| **1** | E. Voice curation + preview | #7 | none |
| **2** | F. E2E "Run all" orchestration | #2 | none |
| **3** | G. Scheduled YouTube upload | #3 | Google Cloud OAuth |

Phase 1 is all local, high-value, no accounts. Phase 2 chains it. Phase 3 adds
the only piece needing an external account.

---

## A. Hinglish support (#1)

Make language a first-class, end-to-end parameter. edge-tts already emits word
timings for Hindi (proven), so captions stay word-synced.

- **`story-to-shotlist-prompt.md`** — add a `## Language` section: when language
  is Hinglish, word budget = `D × 2.0` (≈120 wpm), write narration in **mixed
  Devanagari + English loanwords**, keep English technical nouns in Latin. Add a
  `"language"` field to the output schema.
- **`plan.py`** — add `--language english|hinglish` (default english); pass
  `Language: <x>` in the user message so the single-source prompt does the
  arithmetic. No other logic change.
- **`assemble.py`** — pick the caption font by script: if any caption word
  contains a Devanagari codepoint (U+0900–U+097F), use a Devanagari+Latin font
  (**Kohinoor Devanagari** or **Mukta**, both on macOS) for the `Base`/`Hot`
  styles; else keep `DejaVu Sans`. Make `ASS_HEAD` a function of the font name;
  optional `--font` override. (No `--no-align` needed — edge-tts timings cover Hindi.)
- **`narrate.py`** — no change; the UI just selects a Hindi voice.
- **`studio.py`** — a **Language** toggle on the project (english/hinglish),
  stored in `meta.json`, passed to `plan.py --language` and surfaced so the voice
  dropdown defaults to a Hindi voice for Hinglish.

**Verify:** generate a Hinglish script (word_count ≈ 2×seconds), narrate with a
`hi-IN` voice, assemble → captions show Devanagari correctly (no tofu boxes),
audio/caption sync intact.

---

## B. Metadata generator — title, description, hashtags (#2, #4, #6)

One LLM step produces the publish metadata. Reuses `plan.py`'s provider functions
(no new API wiring).

- **New `metadata.py`** — `import plan` and call `plan.call_claude_cli` /
  `call_claude` / `call_gemini` (same `--provider` flags, default `claude-cli`).
  Input: `script.json` (narration + working title + language). Output
  `metadata.json`:
  ```jsonc
  { "title": "<=100 chars, hook-first, no clickbait caps",
    "description": "2–3 line hook + context, then hashtags line",
    "hashtags": ["#..."], "tags": ["youtube keyword tags"],
    "category": "People & Blogs" }
  ```
  A dedicated system prompt (short-form title craft: curiosity gap, concrete
  noun, ≤ ~60 visible chars ideal; language-matched — Hinglish titles in mixed
  script). Defensive JSON parse reuses `plan.parse_json`.
- **`studio.py`** — a **Metadata** stage card: Run, then show editable
  title/description/hashtags fields (`GET/PUT /api/project/<slug>/metadata`),
  mirroring the script view/edit pattern.

**Verify:** run on an existing `script.json` → sensible title/description/hashtags
in the project's language; edits persist to `metadata.json`.

---

## C. Music library (#5)

Royalty-free bed music, ducked under narration (assemble already does the mix).

- **`music/` folder** — curated no-copyright / CC tracks. Document sources in the
  README: **YouTube Audio Library**, Pixabay Music, Uppbeat, Chosic. Explicit
  caveat: real trending/chart audio triggers YouTube Content ID claims,
  demonetization, or strikes — excluded by design.
- **`studio.py`** — Assemble card gains a **Music** picker: *None / Random /
  <specific file>*; passes the chosen file (or a random library pick) to
  `assemble.py --music`. `MUSIC_GAIN` (0.12) already tuned; expose it later if needed.
- **`assemble.py`** — no code change (already supports `--music`); studio just
  supplies the path.

**Verify:** assemble with a library track → narration clearly on top, music
ducked and fading at the end.

---

## D. Multi-gameplay support (#8)

Turn the single clip into a library with variety control.

- **`footage/`** — hold many clips (optionally subfolders by vibe). `assemble.py`
  `build_gameplay()` already random-picks a file and a random in-point.
- **`assemble.py`** — add optional `--clip <name>` to force a specific file
  (keep random as default); optionally track a small `footage/.recent.json` to
  avoid reusing the same file/section back-to-back across projects.
- **`studio.py`** — Assemble card gains a **Gameplay** picker: *Random /
  <specific file>* (lists `footage/`), passed as `--clip`.

**Verify:** with several clips, repeated assembles draw different footage; forcing
a specific clip works.

---

## E. Better voice via curation + preview (#7)

Stay on edge-tts; make choosing the *right* voice easy and repeatable.

- **Curated shortlist** — a small vetted set per language (e.g. English:
  `en-US-*`, expressive; Hinglish: `hi-IN-MadhurNeural`, `hi-IN-SwaraNeural`),
  surfaced at the top of the dropdown. `tts_studio.py` already exists as the
  prosody bench for auditioning rate/pitch/volume.
- **`studio.py`** — add an inline **voice preview** in the Narrate card: a short
  fixed sample synthesized via the existing narrate path so you hear a voice
  before committing; the chosen voice + prosody save to `meta.json` (already
  stored) and drive the full narration.

**Verify:** preview several voices, pick one, full narration uses exactly that
voice/prosody.

---

## F. E2E automation — "Run all" (#2)

One click chains the whole pipeline using the existing job/poll infrastructure.

- **`studio.py`** — a **Run all** button that sequences
  `plan → metadata → narrate → images → assemble` (and, in Phase 3, `→ upload`)
  as ordered background jobs, stop-on-failure, with the current live-log/poll UI
  advancing card by card. Needs the project's context + language + voice + music
  + gameplay choices captured up front (a short "project settings" form).

**Verify:** from a fresh project with context pasted, Run all yields
`final.mp4` + `metadata.json` unattended; a mid-pipeline failure halts cleanly
with the failing stage's log.

---

## G. Scheduled YouTube upload (#3)

The only piece needing an external account. Upload **private** with a scheduled
`publishAt`.

- **Deps** — `google-api-python-client`, `google-auth-oauthlib` (add to a
  `requirements.txt`; note they're not installed yet).
- **One-time setup (documented in README):** Google Cloud project → enable
  **YouTube Data API v3** → create **OAuth client (Desktop)** → download
  `client_secret.json`. First run opens a browser consent; refresh token cached
  to `~/.config/brain-rot/yt_token.json`.
- **New `upload.py`** — YouTube Data API v3 `videos.insert` with
  `snippet{title, description, tags, categoryId}` from `metadata.json`,
  `status{privacyStatus:"private", publishAt:<ISO8601>,
  selfDeclaredMadeForKids:false}`, resumable upload of `final.mp4`. Optional
  thumbnail (`thumbnails.set`) generated from the first image + title overlay.
  Args: `--video`, `--metadata`, `--publish-at`, `--privacy`.
- **`studio.py`** — an **Upload** card: datetime picker for `publishAt`, Upload
  button, returns the video URL/status. Quota note surfaced.
- **Quota caveat:** each upload costs ~1600 units; default 10,000/day ≈ 6
  uploads/day. Document; request more if needed.

**Verify:** upload a test render → appears private on the channel with the correct
scheduled go-live time, title/description/tags populated.

---

## New / changed files at a glance

- **New:** `metadata.py`, `upload.py`, `requirements.txt`, `music/`, expanded
  `footage/`.
- **Changed:** `story-to-shotlist-prompt.md` (Language section), `plan.py`
  (`--language`), `assemble.py` (script-aware caption font, `--clip`, `--font`),
  `studio.py` (language toggle, metadata card, music/gameplay pickers, voice
  preview, Run-all, upload card), `README.md` (music sources, YouTube OAuth
  setup, quota).
- **Reused as-is:** `narrate.py`, `generate_images.py`, `tts_studio.py`,
  `plan.py` provider functions.

## Risks / notes

1. **Music licensing** — only royalty-free/CC tracks go in `music/`; "trendy"
   copyrighted audio is deliberately excluded (Content ID / strikes).
2. **YouTube auto-posting** — mitigated by scheduled *private* uploads; nothing
   goes public without a `publishAt` you set.
3. **YouTube quota** — ~6 uploads/day on default quota.
4. **Hinglish caption font** must be one that renders **both** Devanagari and
   Latin (Kohinoor Devanagari / Mukta) since narration is mixed-script.
5. **Devanagari word-count vs edge-tts tokens** — minor image-cut drift possible
   (as already documented); captions themselves stay accurate.
