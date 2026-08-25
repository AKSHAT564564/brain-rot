#!/usr/bin/env python3
"""
series.py - Story Mode showrunner: long source -> series.json (bible + outline).

    python series.py --source long.txt --seconds 60 --out projects/<slug>/series.json

Sits one level above plan.py. Reads a long piece of source material and splits it
into an ordered set of episodes, each of which plan.py --bible --episode N later
expands into its own script.json. The system prompt is series-outline-prompt.md
(the single source of truth for the outline structure). Reuses plan.py's provider
plumbing (Claude Code CLI by default, Claude/Gemini API optional).

--seconds is a *hint* for the natural episode length; the showrunner decides the
episode count and each episode's real length itself.
"""

import argparse
import json
import random
import sys
from pathlib import Path

import plan   # reuse call_claude / call_gemini / call_claude_cli / parse_json / DEFAULT_MODEL

PROMPT_FILE = Path(__file__).with_name("series-outline-prompt.md")


def build_user(source: str, seconds: int, guidance: str, language: str) -> str:
    user = (f"Language: {language}\n"
            f"Target length per episode (a hint - you decide the real count and "
            f"each episode's natural length): about {seconds} seconds.\n\n")
    if guidance.strip():
        user += ("Extra guidance (tone/angle/how to split - keep the required JSON "
                 f"structure exactly):\n{guidance.strip()}\n\n")
    user += f"Source material:\n{source.strip()}"
    return user


def normalize(spec: dict, language: str) -> dict:
    """Coerce shapes and fill defaults so plan.py --bible can rely on them."""
    eps = spec.get("episodes") or []
    if not eps:
        sys.exit("showrunner returned no episodes")

    # Re-index contiguously from 1 and enforce the first/last connective rules,
    # so a model slip doesn't break the recap/cliffhanger contract downstream.
    for i, ep in enumerate(eps, 1):
        ep["index"] = i
        ep.setdefault("working_title", f"Episode {i}")
        ep["premise"] = str(ep.get("premise", "")).strip()
        ep["segment"] = str(ep.get("segment", "")).strip()
        ep["target_seconds"] = int(ep.get("target_seconds") or 60)
        ep["cliffhanger"] = str(ep.get("cliffhanger", "")).strip()
        ep["recap_hint"] = str(ep.get("recap_hint", "")).strip()
        if not ep["segment"]:
            sys.exit(f"episode {i} has an empty segment")
    eps[0]["recap_hint"] = ""        # nothing precedes episode 1
    eps[-1]["cliffhanger"] = ""      # the finale resolves, it doesn't tease

    seed = spec.get("seed")
    try:
        seed = int(seed)
    except (TypeError, ValueError):
        seed = None
    if not seed or not (1 <= seed <= 2_000_000_000):
        seed = random.randint(1, 2_000_000_000)

    return {
        "series_title": str(spec.get("series_title", "Untitled Series")).strip()
                        or "Untitled Series",
        "genre_mode": str(spec.get("genre_mode", "narrative")).strip() or "narrative",
        "tone": str(spec.get("tone", "")).strip(),
        "language": spec.get("language") or language,
        "essence": str(spec.get("essence", "")).strip(),
        "style_block": str(spec.get("style_block", "")).strip(),
        "characters": spec.get("characters") or {},
        "seed": seed,
        "episodes": eps,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="path to the long source text")
    ap.add_argument("--seconds", type=int, default=60,
                    help="hint for natural episode length")
    ap.add_argument("--guidance", default="", help="optional tone/split guidance")
    ap.add_argument("--language", default="english",
                    choices=["english", "hinglish"])
    ap.add_argument("--provider", default="claude-cli",
                    choices=["claude", "gemini", "claude-cli"])
    ap.add_argument("--model", default=None)
    ap.add_argument("--out", default="out/series.json")
    ap.add_argument("--print", dest="show", action="store_true")
    args = ap.parse_args()

    if not PROMPT_FILE.exists():
        sys.exit(f"system prompt not found: {PROMPT_FILE}")
    system = PROMPT_FILE.read_text()

    src_path = Path(args.source)
    if not src_path.exists():
        sys.exit(f"source file not found: {src_path}")
    source = src_path.read_text()
    if not source.strip():
        sys.exit("source file is empty")

    model = args.model or plan.DEFAULT_MODEL[args.provider]
    user = build_user(source, args.seconds, args.guidance, args.language)
    print(f"showrunner: {args.language} | provider={args.provider} | model={model}",
          file=sys.stderr)
    caller = {"gemini": plan.call_gemini, "claude-cli": plan.call_claude_cli}.get(
        args.provider, plan.call_claude)

    text = caller(system, user, model)
    spec = normalize(plan.parse_json(text), args.language)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(spec, ensure_ascii=False, indent=2))

    eps = spec["episodes"]
    total = sum(e["target_seconds"] for e in eps)
    print(f"wrote {out}  ({len(eps)} episodes, ~{total}s total, "
          f"seed={spec['seed']}, chars={len(spec['characters'])})", file=sys.stderr)
    if args.show:
        print(json.dumps(spec, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
