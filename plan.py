#!/usr/bin/env python3
"""
plan.py - Stage 1: raw story/article -> script.json via the Claude API.

    export ANTHROPIC_API_KEY=...
    python plan.py --context context.txt --seconds 60 --out out/script.json
    python plan.py --context context.txt --guidance "second person, ominous tone"

The system prompt is story-to-shotlist-prompt.md - the single source of truth
for the output structure. It is read at runtime and sent unchanged, so editing
that file changes the plan without touching this script. --guidance is appended
to the user message only (tone / POV / emphasis), never the structure.

After the model returns, we deterministically assign one image filename per
visual (img_001.png ...). The planner prompt does not emit image keys, but both
generate_images.py and assemble.py require them, so we inject them here rather
than trusting the model to number files consistently.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

CLAUDE_URL = "https://api.anthropic.com/v1/messages"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
CLAUDE_MODEL = "claude-sonnet-5"
GEMINI_MODEL = "gemini-2.0-flash"   # 2.5-flash alias 404s for newer keys; override with --model
CLAUDE_CLI_MODEL = "opus"           # Claude Code model alias -> claude-opus-5 (subscription auth, no API key)
CLAUDE_CLI_EFFORT = "high"          # reasoning effort for the CLI (low|medium|high|xhigh|max)
DEFAULT_MODEL = {"claude": CLAUDE_MODEL, "gemini": GEMINI_MODEL,
                 "claude-cli": CLAUDE_CLI_MODEL}
MAX_TOKENS = 8000
PROMPT_FILE = Path(__file__).with_name("story-to-shotlist-prompt.md")


def norm(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace - matches assemble.py so
    the narration == beats-joined check here agrees with the one there."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower())).strip()


def build_user(context: str, seconds: int, guidance: str, language: str) -> str:
    """The user turn - identical for either provider; the system prompt
    (story-to-shotlist-prompt.md) carries the structure."""
    user = f"Language: {language}\nTarget duration: {seconds} seconds.\n\n"
    if guidance.strip():
        user += ("Extra narration guidance (tone/POV/emphasis only - keep the "
                 f"required JSON structure exactly):\n{guidance.strip()}\n\n")
    user += f"Context:\n{context.strip()}"
    return user


def load_episode(bible_path: str, index: int):
    """Load series.json and return (bible, episode). 1-based index."""
    bible = json.loads(Path(bible_path).read_text())
    eps = bible.get("episodes") or []
    if not (1 <= index <= len(eps)):
        sys.exit(f"--episode {index} out of range (series has {len(eps)} episodes)")
    return bible, eps[index - 1]


def build_episode_user(bible: dict, ep: dict, guidance: str, language: str) -> str:
    """User turn for Story Mode: this episode's segment plus the continuity the
    system prompt can't know - series position, recap, cliffhanger, and the
    locked look. The structure still comes from story-to-shotlist-prompt.md."""
    n, total = ep["index"], len(bible["episodes"])
    L = [f"Language: {language}",
         f"Target duration: {ep['target_seconds']} seconds.",
         "",
         f'This is Episode {n} of {total} in a series titled "{bible["series_title"]}".']
    if bible.get("tone"):
        L.append(f"Series tone: {bible['tone']}.")

    if ep.get("recap_hint"):
        L += ["",
              "The FIRST beat is a ~2-second cold re-entry for viewers who missed "
              f'earlier parts, reminding them of: "{ep["recap_hint"]}". Keep it to '
              'one short beat and tag that beat with "role": "recap".']
    else:
        L += ["", "This is the first episode - no recap. The opening line is the "
                  "strongest hook of the whole series."]

    if ep.get("cliffhanger"):
        L += ["",
              "The FINAL beat lands this cliffhanger so it pulls the viewer to the "
              f'next episode: "{ep["cliffhanger"]}". Tag that final beat with '
              '"role": "cliffhanger".']
    else:
        L += ["", "This is the final episode - no cliffhanger. End on the series "
                  "payoff; resolve, do not tease."]

    L += ["",
          "Use this EXACT style_block verbatim in every image prompt - do not invent "
          "a new look:",
          bible["style_block"]]
    if bible.get("characters"):
        L += ["",
              "Recurring characters - use these exact descriptions verbatim wherever "
              "they appear:",
              json.dumps(bible["characters"], ensure_ascii=False)]

    if guidance.strip():
        L += ["", "Extra narration guidance (tone/POV/emphasis only - keep the "
                  f"required JSON structure exactly):\n{guidance.strip()}"]

    L += ["",
          "Context - this episode's portion of the story. Stay strictly inside it, "
          "invent nothing:",
          ep["segment"].strip()]
    return "\n".join(L)


def _post(url, payload, headers, label) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"content-type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"{label} HTTP {e.code}: {e.read()[:400].decode(errors='replace')}")
    except urllib.error.URLError as e:
        sys.exit(f"could not reach {label}: {e.reason}")


def call_claude(system: str, user: str, model: str) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("set ANTHROPIC_API_KEY (console.anthropic.com -> API keys)")

    data = _post(CLAUDE_URL, {
        "model": model, "max_tokens": MAX_TOKENS, "system": system,
        "messages": [{"role": "user", "content": user}],
    }, {"x-api-key": key, "anthropic-version": "2023-06-01"}, "Claude API")

    blocks = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    text = "".join(blocks).strip()
    if not text:
        sys.exit(f"empty response from Claude: {str(data)[:300]}")
    return text


def call_gemini(system: str, user: str, model: str) -> str:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        sys.exit("set GEMINI_API_KEY (aistudio.google.com/apikey)")

    # responseMimeType=application/json makes Gemini return bare JSON, matching
    # what the prompt asks for (no code fences to strip).
    data = _post(GEMINI_URL.format(model=model), {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"maxOutputTokens": MAX_TOKENS,
                             "responseMimeType": "application/json"},
    }, {"x-goog-api-key": key}, "Gemini API")

    cands = data.get("candidates") or []
    if not cands:
        sys.exit(f"empty response from Gemini: {str(data)[:300]}")
    parts = cands[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        sys.exit(f"no text in Gemini response (finishReason="
                 f"{cands[0].get('finishReason')}): {str(data)[:300]}")
    return text


def call_claude_cli(system: str, user: str, model: str,
                    effort: str = CLAUDE_CLI_EFFORT) -> str:
    """Run the Claude Code CLI headless (`claude -p`). Uses the local
    subscription login instead of a metered API key. `system` replaces Claude
    Code's coding-agent framing (so this works for any prompt, not just the
    shotlist one); the user turn is piped on stdin so long inputs don't hit argv
    limits. `effort` sets the reasoning effort (opus + high == "opus-5-high")."""
    exe = shutil.which("claude")
    if not exe:
        sys.exit("claude CLI not on PATH - install Claude Code, or use "
                 "--provider claude / gemini")

    cmd = [exe, "-p",
           "--system-prompt", system,
           "--output-format", "json",
           "--model", model,
           "--effort", effort]
    # The whole point of this provider is subscription auth. If ANTHROPIC_API_KEY
    # (or a token) is in the environment - e.g. studio.py loaded it from .env for
    # the API provider - the CLI prefers it over the claude.ai login and fails on
    # a depleted key. Strip those for this subprocess so the subscription is used.
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    try:
        p = subprocess.run(cmd, input=user, capture_output=True, text=True,
                           timeout=300, env=env)
    except subprocess.TimeoutExpired:
        sys.exit("claude CLI timed out after 300s")
    if p.returncode != 0:
        sys.exit(f"claude CLI failed (exit {p.returncode}): "
                 f"{(p.stderr or p.stdout)[-500:]}")

    try:
        env = json.loads(p.stdout)
    except json.JSONDecodeError:
        sys.exit(f"claude CLI did not return JSON:\n{p.stdout[:500]}")
    if env.get("is_error"):
        sys.exit(f"claude CLI error ({env.get('subtype')}): "
                 f"{str(env.get('result'))[:500]}")

    text = (env.get("result") or "").strip()
    if not text:
        sys.exit(f"empty result from claude CLI: {str(env)[:300]}")
    return text


def parse_json(text: str) -> dict:
    """The prompt asks for bare JSON, but strip code fences defensively and fall
    back to the outermost {...} if the model wrapped it in prose."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            sys.exit(f"model did not return JSON:\n{text[:500]}")
        return json.loads(m.group(0))


def assign_images(spec: dict) -> int:
    """One sequential image per visual. Deterministic so re-runs are stable and
    generate_images.py / assemble.py get the image keys they require."""
    n = 0
    for beat in spec.get("beats", []):
        for v in beat.get("visuals", []):
            n += 1
            v["image"] = f"img_{n:03d}.png"
    return n


def validate(spec: dict):
    problems = []
    for field in ("title", "beats", "narration"):
        if field not in spec:
            problems.append(f"missing top-level field: {field}")
    if problems:
        sys.exit("script.json is malformed:\n  " + "\n  ".join(problems))

    for b in spec["beats"]:
        for i, v in enumerate(b.get("visuals", [])):
            for k in ("prompt", "motion", "seconds"):
                if k not in v:
                    problems.append(f"beat {b.get('id')} visual {i}: missing '{k}'")

    joined = " ".join(b.get("text", "") for b in spec["beats"])
    if norm(joined) != norm(spec["narration"]):
        problems.append(
            f"narration != beats joined "
            f"({len(norm(spec['narration']).split())} vs {len(norm(joined).split())} words)")

    budget = (spec.get("plan") or {}).get("word_budget")
    wc = len(norm(spec["narration"]).split())
    if budget and wc > budget:
        # a soft warning: overshoot means the video runs long, but it still assembles
        print(f"WARNING: word_count {wc} exceeds budget {budget} - video may run long.",
              file=sys.stderr)

    if problems:
        sys.exit("script.json failed validation:\n  " + "\n  ".join(problems))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--context", help="path to the raw story/article text "
                    "(omit when using --bible/--episode)")
    ap.add_argument("--bible", help="series.json from series.py (Story Mode)")
    ap.add_argument("--episode", type=int, help="1-based episode index within --bible")
    ap.add_argument("--seconds", type=int, default=60, help="target duration")
    ap.add_argument("--guidance", default="", help="optional narration tone/POV guidance")
    ap.add_argument("--language", default="english",
                    choices=["english", "hinglish"],
                    help="narration language (image prompts stay English)")
    ap.add_argument("--provider", default="claude",
                    choices=["claude", "gemini", "claude-cli"])
    ap.add_argument("--model", default=None,
                    help="override the default model for the chosen provider")
    ap.add_argument("--out", default="out/script.json")
    ap.add_argument("--print", dest="show", action="store_true",
                    help="also print the script.json to stdout")
    args = ap.parse_args()

    if not PROMPT_FILE.exists():
        sys.exit(f"system prompt not found: {PROMPT_FILE}")
    system = PROMPT_FILE.read_text()

    # Two modes: a standalone short (--context) or one episode of a series
    # (--bible --episode). Episode mode takes its context, language, duration and
    # look from the series bible so every episode stays consistent.
    bible = ep = None
    if args.bible or args.episode:
        if not (args.bible and args.episode):
            sys.exit("--bible and --episode must be used together")
        bible, ep = load_episode(args.bible, args.episode)
        language = bible.get("language", args.language)
        seconds = ep["target_seconds"]
        user = build_episode_user(bible, ep, args.guidance, language)
        label = f"episode {ep['index']}/{len(bible['episodes'])}"
    else:
        if not args.context:
            sys.exit("give --context (standalone) or --bible + --episode (series)")
        ctx_path = Path(args.context)
        if not ctx_path.exists():
            sys.exit(f"context file not found: {ctx_path}")
        context = ctx_path.read_text()
        if not context.strip():
            sys.exit("context file is empty")
        language, seconds = args.language, args.seconds
        user = build_user(context, seconds, args.guidance, language)
        label = "standalone"

    model = args.model or DEFAULT_MODEL[args.provider]
    print(f"planning: {label} | {seconds}s | {language} | "
          f"provider={args.provider} | model={model}", file=sys.stderr)
    caller = {"gemini": call_gemini, "claude-cli": call_claude_cli}.get(
        args.provider, call_claude)
    text = caller(system, user, model)
    spec = parse_json(text)

    # In episode mode, lock the shared identity deterministically rather than
    # trusting the model to echo it verbatim - this is what keeps episodes
    # visually consistent regardless of model drift.
    if bible:
        spec["style_block"] = bible["style_block"]
        if bible.get("characters"):
            spec["characters"] = bible["characters"]
        spec["language"] = language
        spec["seed"] = bible["seed"]        # generate_images.py picks this up
        spec["series"] = {"title": bible["series_title"], "index": ep["index"],
                          "total": len(bible["episodes"])}

    n_imgs = assign_images(spec)
    validate(spec)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(spec, ensure_ascii=False, indent=2))

    beats = len(spec["beats"])
    wc = len(norm(spec["narration"]).split())
    print(f"wrote {out}  ({beats} beats, {n_imgs} visuals, {wc} words)", file=sys.stderr)
    if args.show:
        print(json.dumps(spec, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
