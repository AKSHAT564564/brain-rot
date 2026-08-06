# Story Mode — long script → a series of linked shorts

## The idea

Today the pipeline turns one piece of context into **one** short. Story Mode adds a
layer *above* that: paste a long script of any genre — a short story, a chapter, a
historical saga, a long explainer, a true-crime write-up — and the system breaks it
into an ordered **series of shorts** (Ep 1…N) that are individually watchable but
add up to the whole, with consistent look, characters, and tone across every episode
and cliffhangers pulling the viewer to the next one.

Think of it as a **showrunner** step that sits before the existing five-stage
pipeline. Each episode, once outlined, is just a normal project that runs through
Plan → Narrate → Images → Metadata → Assemble unchanged. So Story Mode is mostly
*orchestration + continuity*, not a new renderer.

---

## Why this fits the current architecture cleanly

Two things already in the codebase make a series practical rather than a rewrite:

1. **Shared seed + verbatim style block already exist.** `generate_images.py`
   derives one stable seed per video (`seed_for(title)`) and appends the
   `style_block` + character descriptions to every prompt verbatim — the exact
   mechanism that stops images drifting. If every episode inherits the **same
   style block, same character bible, and the same base seed**, cross-*episode*
   visual consistency comes almost for free.
2. **Each stage already operates on a self-contained project dir** with a
   `script.json`. An episode is just such a dir. So episodes reuse `narrate.py`,
   `generate_images.py`, `metadata.py`, `assemble.py` with zero changes.

The genuinely new work is: (a) splitting the long script into episodes, and (b)
writing each episode's `script.json` *with continuity context* so it doesn't
re-invent the look or forget what happened last episode.

---

## The two-pass LLM model

### Pass A — the Showrunner (`series.py`, new)

Input: the long source + target episode length + optional episode count.
Output: a **series bible + outline** (`series.json`), no per-shot detail yet.

The showrunner does the expensive, once-per-series decisions:

- **Split the source into episodes.** Semantic boundaries, not mechanical word
  chunks — cut on natural turning points (a scene change, a reveal, a "meanwhile",
  a logical section) — but constrained by a duration guardrail so each episode
  lands near the target length (`word_budget` per episode drives how many episodes
  the source needs). It returns, per episode: the *segment of the source it
  covers*, a one-line premise, and the **cliffhanger** it ends on.
- **Lock the series bible** (reused verbatim by every episode):
  - `style_block` — the one-sentence visual identity, decided once.
  - `characters` — fixed physical descriptions for anyone who recurs *across
    episodes* (this is where the faceless-design rule matters most — see Risks).
  - `tone` / genre, `series_title`, and a **base `seed`** for image generation.
- **Define the series essence** — the throughline the whole series must deliver,
  extending the prompt's existing "prime directive — keep the essence" from one
  video to the arc across all of them.

### Pass B — the Episode Writer (extend `plan.py`)

For each episode, produce a normal `script.json`, but seeded with continuity:

- The **series bible** (style block + characters + seed) is injected, not
  re-derived — so `plan.py` uses the locked look instead of inventing a new one.
- A short **"previously"** recap of the prior episode and the **incoming
  cliffhanger** are passed as context so the narration re-enters cleanly and pays
  the cliffhanger off.
- Only this episode's **source segment** is the context — the essence directive
  applies at episode level (deliver this segment's core) *and* series level
  (advance the throughline).
- **Tag continuity beats.** The recap beat(s) and the cliffhanger beat get a flag
  in `script.json` (e.g. `"role": "recap" | "cliffhanger"`). Harmless for the short
  itself, but it's what later lets the long-form "binge cut" strip them for a
  seamless throughline — a one-field change now that saves a re-scripting job later.
- Narration shape per episode: fast cold re-entry (except Ep 1, which is the
  strongest hook of the whole series) → the segment's mini-arc → end on the
  cliffhanger that sells the next episode. No "like and subscribe"; the cliffhanger
  is the retention device.

---

## Data model & file layout

A series is a parent of mini-projects. Nest episodes as normal project dirs so
every existing stage works unchanged:

```
projects/<series-slug>/
  series.json            # bible + outline: series_title, genre, tone, style_block,
                         #   characters, base seed, essence, episodes[] (premise,
                         #   source-segment, cliffhanger, status)
  source.txt             # the full long script (provenance)
  logs/series.log        # showrunner output
  episodes/
    ep01/                # a full mini-project — identical shape to today's projects/<slug>/
      meta.json          #   + series_slug, ep_index, prev/next pointers
      script.json  narration.mp3  narration.words.json
      images/  metadata.json  logs/  final.mp4
    ep02/
    …
```

`series.json.episodes[i]` and `episodes/epNN/` stay in sync; the UI reads the
outline from `series.json` and per-episode artifact status from each `epNN/`.

---

## New & modified files

**New**
- `series.py` — the showrunner. `--source long.txt --seconds 60 [--episodes N]
  [--language …] [--provider …] --out projects/<slug>/series.json`. Reuses
  `plan.py`'s provider plumbing (`call_claude_cli` etc.) and `parse_json`.
- `series-outline-prompt.md` — the showrunner system prompt (split rules,
  cliffhanger craft, bible extraction, series-essence). Sibling to
  `story-to-shotlist-prompt.md`, same "single source of truth" pattern.

**Modified**
- `plan.py` — accept an optional `--bible path/to/series.json` + `--episode N`.
  When present: use the bible's `style_block`/`characters`/`seed`, restrict context
  to that episode's segment, and inject the recap + cliffhanger into the user turn.
  Standalone behaviour (no `--bible`) is unchanged.
- `generate_images.py` — add `--seed` to override `seed_for(title)`, so all
  episodes in a series share one base seed (per-image variation still comes from
  the prompt). Small, backward-compatible.
- `metadata.py` — series-aware: title format `"<Series> — Ep N: <hook>"`, shared
  tags, and prev/next lines in the description. (English-only rule already in place.)
- `studio.py` — the Story Mode UI + batch orchestration (below).

---

## UI — Story Mode in `studio.py`

- **New project type toggle:** *Single short* (today) vs *Story / Series*.
- **Series creator:** paste the long script, pick target episode length and
  either an explicit episode count or "auto". Run the **Showrunner** → renders the
  editable **outline**: a list of episode cards (premise, source segment,
  cliffhanger) plus the locked bible (style block, characters, tone, seed). The
  user can edit boundaries, reorder, merge/split, or tweak the bible before
  committing — this is the cheap place to fix the series shape.
- **Episode workspace:** expanding an episode reveals the familiar five stage
  cards, operating on `episodes/epNN/`. Continuity fields are pre-filled from the
  bible and shown read-only (with an "override for this episode" escape hatch).
- **Batch run:** "Generate all episodes" chains Plan→Images→Narrate→Metadata→
  Assemble across every episode as ordered background jobs, stop-on-failure, with a
  per-episode progress grid. (This subsumes the pending "Run all" from
  `improvements-plan.md` — build it once, at the series level.)

---

## Metadata, playlists & release

- Consistent titling and a shared tag set make the episodes a coherent **YouTube
  playlist / series**; descriptions cross-link "Part N of M" and point to the next.
- Pairs naturally with the deferred **scheduled-upload** work (`improvements-plan.md`
  Phase 3): a series wants a **staggered release** (one episode/day) — upload all
  episodes private with incrementing `publishAt` dates. Story Mode is the feature
  that makes scheduled upload actually valuable.

---

## Long-form compilation — the "binge cut"

When every episode is assembled, stitch them into one continuous long-form video —
a 5–15 min "director's cut" for viewers who don't want to wait for the daily drops,
and a **second monetization surface** (long-form ads / watch-time) from content you
already rendered.

**The catch: episodes are written to stand alone.** Each opens with a cold re-entry
/ "previously…" and ends on a cliffhanger. Concatenated raw, those seams play as a
constant recap-then-tease stutter — fine between daily uploads, jarring in one
sitting. Two ways to handle it:

- **(A) Straight concat — v1.** `ffmpeg`-concat the episode `final.mp4`s as-is, add
  a series title card, per-episode chapter markers, and a single continuous music
  bed. Zero re-render, ships fast. Accepts mild recap/cliffhanger redundancy
  (already softened by the "auto" cliffhanger rule on non-suspense sources).
- **(B) Seamless re-assembly — v2.** Drop the recap + cliffhanger for a clean
  throughline. Enabled by the `role` beat-tags added in Pass B: the compiler
  re-runs assembly over the **union** of all episodes' beats / images / narration
  with the tagged beats skipped. Captions are word-timed, so trimming a tagged
  beat's span is exact. Result: one seamless film, no stutter.

**Orientation.** The layout is inherently vertical (AI image stacked over gameplay).
v1 keeps the long cut **vertical** — YouTube accepts vertical long-form (letterboxed
on desktop). A true 16:9 reframe is a different layout problem (image full-frame
with gameplay as picture-in-picture, or side-by-side) and is out of scope for now;
noted as a future option.

**Extras that make it read as one film:**
- Auto **chapter timestamps** (each episode's duration is known) → written to a
  `chapters.txt` for the YouTube description; optional on-screen chapter cards.
- One **continuous music bed** across the whole runtime instead of per-episode.
- A single series title intro and one outro.

**Output:** `projects/<series-slug>/series-final.mp4` + `chapters.txt`, exposed as a
series-level **"Compile long-form"** action in the UI, unlocked once all episodes
are assembled.

---

## Genre-agnostic splitting

The showrunner prompt must handle both modes the source might be:

- **Narrative** (fiction, true crime, history-as-story): split on plot turns; each
  episode ends on a genuine cliffhanger (a reveal withheld, a decision pending).
- **Expository** (explainers, "the 7 weirdest…", how-something-works): split on
  logical sections; the "cliffhanger" becomes a curiosity gap — *"but the part
  everyone gets wrong is next."*

Same bible/continuity machinery either way; only the boundary logic and hook style
differ, and the prompt picks based on the source.

---

## Risks & limitations

1. **Cross-episode visual continuity is the hard part.** A shared seed + verbatim
   style block + character bible get us most of the way, but generated faces still
   won't survive across dozens of images in many episodes. Mitigation: lean *harder*
   on the existing faceless-design rule (backs of heads, hands, silhouettes,
   object stand-ins) — a series makes that rule non-negotiable, not optional. Flag
   in the outline any episode that would need a recurring recognizable face.
2. **Arbitrary-feeling boundaries** if the source doesn't have clean seams. The
   editable outline (human-in-the-loop before committing) is the safety valve.
3. **Cost/quota scales with episode count** — N episodes = N× images + TTS. The
   outline should show an estimated image count up front; batch runs should respect
   image-provider rate limits (and now degrade gracefully thanks to the
   missing-image fallback already added to `assemble.py`).
4. **Cliffhanger fatigue** — forced cliffhangers on a non-suspense source feel
   cheap. The prompt should allow a softer "thread pull" for expository series.
5. **Essence at two scales** — an episode can nail its own segment yet the series
   throughline still fray. The series-essence field + a final "does the arc land
   across all episodes?" check in the showrunner prompt guard this.

---

## Phased build

Full-UI from the start (per decisions below) — the engine and its UI ship together
each phase rather than a CLI-only stage.

- **Phase 1 — Series engine + creator UI.** `series.py` + `series-outline-prompt.md`;
  `plan.py --bible/--episode` (with `role` beat-tags); `generate_images.py --seed`;
  and the `studio.py` Story Mode creator — paste long script → Showrunner → editable
  outline + locked bible → per-episode five-stage workspace. Prove continuity on a
  ~3-episode series: look + characters hold, cliffhangers connect.
- **Phase 2 — Batch orchestration.** "Generate all episodes" — ordered background
  jobs across every episode, stop-on-failure, per-episode progress grid (subsumes
  the pending Run-all from `improvements-plan.md`).
- **Phase 3 — Long-form compilation.** The "binge cut": v1 straight concat with
  title card + chapters + continuous music; v2 seamless re-assembly using the
  `role` tags. Outputs `series-final.mp4` + `chapters.txt`.
- **Phase 4 — Series release.** Series-aware metadata/playlist cross-linking +
  staggered scheduled upload of the episodes (one/day) and the long-form cut
  (joins `improvements-plan.md` Phase 3).

---

## Resolved decisions

1. **Episode count → auto.** The showrunner decides the count from the source's
   natural length; no user-set count required (an override can come later if wanted).
2. **Episode length → natural.** Episodes vary with their segment; not forced to a
   single fixed duration. Chapters in the long cut inherit those natural lengths.
3. **Cliffhangers → auto.** Hardness picked from genre — real cliffhangers for
   narrative, softer curiosity-gap "thread pulls" for expository sources.
4. **First deliverable → full UI.** Build the engine and Story Mode UI together in
   Phase 1; no CLI-only stage.

Longer term (noted, not scheduled): 16:9 long-form reframe; per-episode continuity
overrides; recurring-face handling beyond faceless design.
