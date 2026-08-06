#!/usr/bin/env python3
"""
metadata.py - Stage: script.json -> metadata.json (YouTube title/description/tags).

    python metadata.py --script projects/<slug>/script.json \
                       --out projects/<slug>/metadata.json

Reuses plan.py's provider plumbing (same --provider / --model flags, default the
Claude Code CLI on your subscription). Produces the publish metadata a short
needs: a hook-first title, a description, hashtags and SEO tags - in the same
language as the narration.
"""

import argparse
import json
import sys
from pathlib import Path

import plan   # reuse call_claude / call_gemini / call_claude_cli / parse_json

SYSTEM = """\
You write publishing metadata for short-form vertical videos (YouTube Shorts / \
TikTok / Reels). You are given a video's narration and working title. You output \
JSON and nothing else.

Return exactly this object:

{
  "title": "hook-first title, <= 90 characters, ideally <= 60 visible",
  "description": "2-3 short lines: a hook, then one line of context. Then a blank line, then the hashtags on one line.",
  "hashtags": ["#lowercase", "#nospaces"],
  "tags": ["seo keyword phrase", "..."],
  "category": "one of: Education, Entertainment, People & Blogs, News & Politics, Science & Technology, Gaming, Howto & Style"
}

Rules:
- ALWAYS write the metadata (title, description, hashtags, tags) in the Latin \
alphabet - English. Never use Devanagari in the metadata. If the narration is \
Hinglish (mixed Devanagari and English), romanise the Hindi into Latin letters \
(e.g. narration "तीन astronauts" -> title "Teen astronauts", "साल 1969" -> "Saal \
1969") so the whole title/description reads in Latin script, mixing romanised \
Hindi and English words the way people type Hinglish. Do NOT translate to pure \
English and do NOT output any Devanagari characters.
- Title: curiosity gap or a concrete, surprising specific. No ALL-CAPS clickbait, \
no emoji spam (at most one emoji), no "you won't believe". Front-load the hook.
- Description: no "in this video", no "subscribe/follow" call to action in the \
first two lines. End the description block with the hashtags line.
- hashtags: 3-6, lowercase, no spaces, relevant to the actual content (not \
generic #fyp padding only - one or two broad tags are fine).
- tags: 8-15 search phrases a viewer might type. Lowercase, no '#'.
- Invent nothing that isn't supported by the narration.
Return only the JSON object. No prose, no code fences.\
"""


def build_user(spec: dict) -> str:
    parts = [f"Working title: {spec.get('title', '')}",
             f"Language: {spec.get('language', 'english')}",
             f"Target seconds: {spec.get('target_seconds', '')}",
             "", "Narration:", spec.get("narration", "").strip()]
    return "\n".join(parts)


def normalize(meta: dict) -> dict:
    """Coerce to the expected shapes so downstream (and the UI) can rely on them."""
    out = {
        "title": str(meta.get("title", "")).strip(),
        "description": str(meta.get("description", "")).strip(),
        "hashtags": meta.get("hashtags") or [],
        "tags": meta.get("tags") or [],
        "category": str(meta.get("category", "People & Blogs")).strip()
                    or "People & Blogs",
    }
    # hashtags: ensure each starts with '#', no spaces
    out["hashtags"] = [("#" + h.lstrip("#").replace(" ", ""))
                       for h in out["hashtags"] if str(h).strip()]
    out["tags"] = [str(t).strip() for t in out["tags"] if str(t).strip()]
    if not out["title"]:
        sys.exit("model returned no title")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", required=True, help="script.json (reads narration+title)")
    ap.add_argument("--provider", default="claude-cli",
                    choices=["claude", "gemini", "claude-cli"])
    ap.add_argument("--model", default=None)
    ap.add_argument("--out", default="out/metadata.json")
    ap.add_argument("--print", dest="show", action="store_true")
    args = ap.parse_args()

    spec = json.loads(Path(args.script).read_text())
    if not (spec.get("narration") or "").strip():
        sys.exit(f"{args.script} has no narration")

    model = args.model or plan.DEFAULT_MODEL[args.provider]
    user = build_user(spec)
    caller = {"gemini": plan.call_gemini, "claude-cli": plan.call_claude_cli}.get(
        args.provider, plan.call_claude)
    print(f"metadata: {spec.get('language', 'english')} | provider={args.provider} "
          f"| model={model}", file=sys.stderr)

    text = caller(SYSTEM, user, model)
    meta = normalize(plan.parse_json(text))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    print(f"wrote {out}  (title: {meta['title'][:60]!r}, "
          f"{len(meta['hashtags'])} hashtags, {len(meta['tags'])} tags)",
          file=sys.stderr)
    if args.show:
        print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
