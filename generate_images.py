#!/usr/bin/env python3
"""
generate_images.py - script.json -> the PNGs assemble.py expects.

    export CF_ACCOUNT_ID=...  CF_API_TOKEN=...
    python generate_images.py --script out/script.json --out out/images

Providers:
    cloudflare   FLUX.1 Schnell, ~230 images/day free, no card      (default)
    fal          FLUX.1 Schnell, ~$0.003/MP, faster                 FAL_KEY
    pollinations no key at all, demo grade - useful for smoke tests

Every prompt gets the style_block and any character descriptions appended
verbatim, and the whole video shares one seed. Those two things are what keep
independently generated images from drifting apart.
"""

import argparse
import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TIMEOUT = 180
RETRIES = 3


# ------------------------------------------------------------------ helpers

def post_json(url, payload, headers, timeout=TIMEOUT):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def seed_for(title: str) -> int:
    """Stable seed derived from the video title, so re-runs reproduce."""
    return int(hashlib.sha256(title.encode()).hexdigest()[:8], 16) % (2**31)


# ---------------------------------------------------------------- providers

def gen_cloudflare(prompt, seed, size, steps=4):
    acct = os.environ.get("CF_ACCOUNT_ID")
    token = os.environ.get("CF_API_TOKEN")
    if not (acct and token):
        sys.exit("set CF_ACCOUNT_ID and CF_API_TOKEN "
                 "(dash.cloudflare.com -> AI -> Workers AI)")

    url = (f"https://api.cloudflare.com/client/v4/accounts/{acct}"
           f"/ai/run/@cf/black-forest-labs/flux-1-schnell")
    data = post_json(url, {"prompt": prompt, "steps": steps, "seed": seed},
                     {"Authorization": f"Bearer {token}"})

    if not data.get("success", True):
        raise RuntimeError(str(data.get("errors") or data))
    img = data.get("result", {}).get("image")
    if not img:
        raise RuntimeError(f"no image in response: {str(data)[:200]}")
    return base64.b64decode(img)


def gen_fal(prompt, seed, size, steps=4):
    key = os.environ.get("FAL_KEY")
    if not key:
        sys.exit("set FAL_KEY (fal.ai/dashboard/keys)")

    w, h = size
    data = post_json(
        "https://fal.run/fal-ai/flux/schnell",
        {"prompt": prompt, "seed": seed, "num_inference_steps": steps,
         "image_size": {"width": w, "height": h}, "num_images": 1,
         "enable_safety_checker": False},
        {"Authorization": f"Key {key}"})

    imgs = data.get("images") or []
    if not imgs:
        raise RuntimeError(f"no image in response: {str(data)[:200]}")
    with urllib.request.urlopen(imgs[0]["url"], timeout=TIMEOUT) as r:
        return r.read()


def gen_pollinations(prompt, seed, size, steps=4):
    w, h = size
    q = urllib.parse.urlencode(
        {"width": w, "height": h, "seed": seed, "nologo": "true",
         "model": "flux"})
    url = (f"https://image.pollinations.ai/prompt/"
           f"{urllib.parse.quote(prompt)}?{q}")
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
        return r.read()


PROVIDERS = {
    "cloudflare": gen_cloudflare,
    "fal": gen_fal,
    "pollinations": gen_pollinations,
}


# ------------------------------------------------------------ prompt assembly

def build_prompt(visual, spec):
    """Append the style block and any character descriptions verbatim.

    Verbatim matters: paraphrasing a character description produces a
    different-looking person, and viewers notice immediately."""
    parts = [visual["prompt"].strip().rstrip(".")]

    style = (spec.get("style_block") or "").strip()
    if style and style.lower() not in parts[0].lower():
        parts.append(style)

    for name, desc in (spec.get("characters") or {}).items():
        if name.lower() in visual["prompt"].lower() and desc not in parts[0]:
            parts.append(desc.strip())

    return ", ".join(p for p in parts if p)


def collect(spec):
    """One entry per distinct image file.

    Several visuals deliberately share an image file - different Ken Burns
    moves on the same still - so dedupe or you pay to generate it twice."""
    seen, out = {}, []
    for beat in spec["beats"]:
        for v in beat.get("visuals", []):
            fn = v.get("image")
            if not fn:
                sys.exit(f"beat {beat.get('id')}: visual missing 'image'")
            if fn in seen:
                continue
            seen[fn] = True
            out.append((fn, build_prompt(v, spec)))
    return out


# -------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", help="script.json (omit if using --prompt)")
    ap.add_argument("--prompt", help="generate a single image from this text")
    ap.add_argument("--out", default="out/images")
    ap.add_argument("--provider", default="cloudflare", choices=list(PROVIDERS))
    ap.add_argument("--size", default="1024x1024",
                    help="square matches the 1080x960 panel with minimal crop")
    ap.add_argument("--steps", type=int, default=4)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--force", action="store_true", help="regenerate existing")
    ap.add_argument("--dry-run", action="store_true",
                    help="print assembled prompts, generate nothing")
    args = ap.parse_args()

    if not (args.script or args.prompt):
        sys.exit("give either --script or --prompt")

    w, h = (int(x) for x in args.size.lower().split("x"))

    # --- single image mode --------------------------------------------
    if args.prompt:
        seed = args.seed if args.seed is not None else seed_for(args.prompt)
        outdir = Path(args.out)
        outdir.mkdir(parents=True, exist_ok=True)
        path = outdir / "test.png"

        print(f"provider={args.provider} | {w}x{h} | seed={seed}\n{args.prompt}\n")
        if args.dry_run:
            return
        try:
            blob = PROVIDERS[args.provider](args.prompt, seed, (w, h), args.steps)
        except urllib.error.HTTPError as e:
            sys.exit(f"HTTP {e.code}: {e.read()[:300].decode(errors='replace')}")
        path.write_bytes(blob)
        print(f"wrote {path}  ({len(blob)/1024:.0f} KB)")
        return

    spec = json.loads(Path(args.script).read_text())
    seed = args.seed if args.seed is not None else seed_for(spec.get("title", "untitled"))
    jobs = collect(spec)

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"{len(jobs)} distinct images | provider={args.provider} "
          f"| {w}x{h} | seed={seed}\n")

    if args.dry_run:
        for fn, prompt in jobs:
            print(f"--- {fn}\n{prompt}\n")
        return

    gen = PROVIDERS[args.provider]
    made = skipped = failed = 0

    for i, (fn, prompt) in enumerate(jobs, 1):
        path = outdir / fn
        if path.exists() and not args.force:
            print(f"[{i}/{len(jobs)}] {fn}  exists, skipping")
            skipped += 1
            continue

        # vary the seed per image but derive it from the video seed, so the
        # set stays reproducible while the images stay distinct
        img_seed = (seed + i * 7919) % (2**31)
        print(f"[{i}/{len(jobs)}] {fn}  ...", end="", flush=True)

        for attempt in range(1, RETRIES + 1):
            try:
                blob = gen(prompt, img_seed, (w, h), args.steps)
                path.write_bytes(blob)
                print(f" {len(blob)/1024:.0f} KB")
                made += 1
                break
            except urllib.error.HTTPError as e:
                body = e.read()[:180].decode(errors="replace")
                if e.code == 429 and attempt < RETRIES:
                    wait = 2 ** attempt * 5
                    print(f" rate limited, waiting {wait}s...", end="", flush=True)
                    time.sleep(wait)
                    continue
                print(f" HTTP {e.code}: {body}")
                failed += 1
                break
            except Exception as e:
                if attempt < RETRIES:
                    time.sleep(2 ** attempt)
                    continue
                print(f" failed: {e}")
                failed += 1
                break

    print(f"\n{made} generated, {skipped} skipped, {failed} failed -> {outdir}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
