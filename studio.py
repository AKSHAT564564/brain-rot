#!/usr/bin/env python3
"""
studio.py - the brain-rot pipeline orchestrator UI.

    python studio.py                # -> http://localhost:7861

One page that runs the whole pipeline per project, keeping each project's
assets under projects/<slug>/ so runs never collide:

    Plan      story/article -> script.json     (plan.py, Claude API)
    Narrate   narration     -> narration.mp3   (narrate.py, edge-tts)
    Images    script.json   -> images/*.png    (generate_images.py, Cloudflare)
    Assemble  everything     -> final.mp4       (assemble.py)

Stages run as background jobs (subprocess in a thread, output tee'd to
projects/<slug>/logs/); the page polls for status and log tail. No dependencies
beyond edge-tts - the server is stdlib http.server, matching tts_studio.py.

Keys are read from a .env file next to this script (loaded into the environment
so the stage subprocesses inherit them):

    ANTHROPIC_API_KEY=...      # plan.py
    CF_ACCOUNT_ID=...          # generate_images.py (Cloudflare)
    CF_API_TOKEN=...
"""

import asyncio
import base64
import json
import mimetypes
import random
import re
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

import edge_tts

PORT = 7861
ROOT = Path(__file__).resolve().parent
PROJECTS = ROOT / "projects"
FOOTAGE = ROOT / "footage"
PY = sys.executable or "python3"

# in-memory job registry: id -> dict(stage, slug, status, rc, log, started)
JOBS = {}
JOBS_LOCK = threading.Lock()
_JOB_SEQ = 0


# ---------------------------------------------------------------- env / util

def load_env():
    """Minimal .env loader (KEY=VALUE per line) - no python-dotenv dependency."""
    import os
    f = ROOT / ".env"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s or "untitled"


def unique_slug(title: str) -> str:
    base = slugify(title)
    slug, n = base, 2
    while (PROJECTS / slug).exists():
        slug = f"{base}-{n}"
        n += 1
    return slug


def pdir(slug: str) -> Path:
    return PROJECTS / slug


def meta_path(slug: str) -> Path:
    return pdir(slug) / "meta.json"


def read_meta(slug: str) -> dict:
    return json.loads(meta_path(slug).read_text())


def write_meta(slug: str, meta: dict):
    meta_path(slug).write_text(json.dumps(meta, ensure_ascii=False, indent=2))


def set_stage(slug: str, stage: str, status: str):
    meta = read_meta(slug)
    meta.setdefault("stages", {})[stage] = {"status": status, "updated": now()}
    if stage == "narrate" and status == "running":
        pass
    write_meta(slug, meta)


def artifacts(slug: str) -> dict:
    d = pdir(slug)
    imgs = d / "images"
    return {
        "script": (d / "script.json").exists(),
        "narration": (d / "narration.mp3").exists(),
        "images": len(list(imgs.glob("*.png"))) if imgs.exists() else 0,
        "metadata": (d / "metadata.json").exists(),
        "final": (d / "final.mp4").exists(),
    }


MUSIC = ROOT / "music"
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


def media_files(folder: Path, exts) -> list:
    if not folder.exists():
        return []
    return sorted(p.name for p in folder.iterdir()
                  if p.suffix.lower() in exts and not p.name.startswith("."))


# ------------------------------------------------------------------- voices

def list_voices():
    async def _v():
        vs = await edge_tts.list_voices()
        return sorted(
            ({"name": v["ShortName"], "locale": v["Locale"], "gender": v["Gender"],
              "traits": ", ".join(v.get("VoicePersonalities") or [])} for v in vs),
            key=lambda v: (v["locale"] != "en-US", not v["locale"].startswith("en"),
                           v["locale"] != "hi-IN", v["locale"], v["name"]))
    return asyncio.run(_v())


# --------------------------------------------------------------- job runner

def start_job(stage: str, slug: str, cmd: list) -> str:
    global _JOB_SEQ
    logdir = pdir(slug) / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    logfile = logdir / f"{stage}.log"

    with JOBS_LOCK:
        _JOB_SEQ += 1
        jid = str(_JOB_SEQ)
        JOBS[jid] = {"stage": stage, "slug": slug, "status": "running",
                     "rc": None, "log": str(logfile), "started": time.time()}

    set_stage(slug, stage, "running")

    def run():
        with open(logfile, "w") as lf:
            lf.write(f"$ {' '.join(str(c) for c in cmd)}\n\n")
            lf.flush()
            try:
                p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT,
                                     cwd=str(ROOT))
                rc = p.wait()
            except Exception as e:
                lf.write(f"\nlauncher error: {e}\n")
                rc = 1
        with JOBS_LOCK:
            JOBS[jid]["status"] = "done" if rc == 0 else "failed"
            JOBS[jid]["rc"] = rc
        set_stage(slug, stage, "done" if rc == 0 else "failed")

    threading.Thread(target=run, daemon=True).start()
    return jid


def tail(path: str, n=200) -> str:
    try:
        lines = Path(path).read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


# ------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    # -- response helpers --
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        self._send(code, {"error": msg})

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    # -- GET --
    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path == "/api/voices":
            try:
                return self._send(200, list_voices())
            except Exception as e:
                return self._err(502, str(e))
        if path == "/api/projects":
            return self._send(200, self._projects())
        if path == "/api/music":
            return self._send(200, media_files(MUSIC, {".mp3", ".m4a", ".wav"}))
        if path == "/api/gameplay":
            return self._send(200, media_files(FOOTAGE, VIDEO_EXTS))

        m = re.match(r"^/api/project/([^/]+)$", path)
        if m:
            return self._project(unquote(m.group(1)))

        m = re.match(r"^/api/project/([^/]+)/script$", path)
        if m:
            return self._get_script(unquote(m.group(1)))

        m = re.match(r"^/api/project/([^/]+)/metadata$", path)
        if m:
            return self._get_json_artifact(unquote(m.group(1)), "metadata.json")

        m = re.match(r"^/api/jobs/(\d+)$", path)
        if m:
            return self._job(m.group(1))

        m = re.match(r"^/file/([^/]+)/(.+)$", path)
        if m:
            return self._file(unquote(m.group(1)), unquote(m.group(2)))

        self._err(404, "not found")

    # -- POST / PUT --
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        body = self._body()

        if path == "/api/projects":
            return self._create(body)
        if path == "/api/voice-preview":
            return self._voice_preview(body)

        m = re.match(r"^/api/project/([^/]+)/(plan|narrate|images|metadata|assemble)$", path)
        if m:
            return self._run_stage(unquote(m.group(1)), m.group(2), body)

        self._err(404, "not found")

    def do_PUT(self):
        path = self.path.split("?", 1)[0]
        m = re.match(r"^/api/project/([^/]+)/script$", path)
        if m:
            return self._put_script(unquote(m.group(1)), self._body())
        m = re.match(r"^/api/project/([^/]+)/metadata$", path)
        if m:
            return self._put_json_artifact(unquote(m.group(1)), "metadata.json",
                                           self._body())
        self._err(404, "not found")

    # -- handlers --
    def _projects(self):
        out = []
        if PROJECTS.exists():
            for d in sorted(PROJECTS.iterdir()):
                if (d / "meta.json").exists():
                    m = json.loads((d / "meta.json").read_text())
                    out.append({"slug": d.name, "title": m.get("title", d.name),
                                "created": m.get("created")})
        return out

    def _create(self, body):
        title = (body.get("title") or "").strip()
        if not title:
            return self._err(400, "title required")
        seconds = int(body.get("seconds") or 60)
        slug = unique_slug(title)
        pdir(slug).mkdir(parents=True, exist_ok=True)
        write_meta(slug, {
            "title": title, "slug": slug, "seconds": seconds,
            "provider": "cloudflare", "voice": None, "created": now(),
            "stages": {},
        })
        self._send(200, {"slug": slug})

    def _project(self, slug):
        if not meta_path(slug).exists():
            return self._err(404, "no such project")
        meta = read_meta(slug)
        meta["artifacts"] = artifacts(slug)
        self._send(200, meta)

    def _get_script(self, slug):
        f = pdir(slug) / "script.json"
        if not f.exists():
            return self._err(404, "no script yet")
        self._send(200, f.read_text(), "application/json")

    def _put_script(self, slug, body):
        script = body.get("script")
        if script is None:
            return self._err(400, "script required")
        try:
            obj = script if isinstance(script, dict) else json.loads(script)
        except json.JSONDecodeError as e:
            return self._err(400, f"invalid JSON: {e}")
        (pdir(slug) / "script.json").write_text(
            json.dumps(obj, ensure_ascii=False, indent=2))
        self._send(200, {"ok": True})

    def _get_json_artifact(self, slug, name):
        f = pdir(slug) / name
        if not f.exists():
            return self._err(404, f"no {name} yet")
        self._send(200, f.read_text(), "application/json")

    def _put_json_artifact(self, slug, name, body):
        obj = body.get("data")
        if obj is None:
            return self._err(400, "data required")
        try:
            obj = obj if isinstance(obj, (dict, list)) else json.loads(obj)
        except json.JSONDecodeError as e:
            return self._err(400, f"invalid JSON: {e}")
        (pdir(slug) / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2))
        self._send(200, {"ok": True})

    def _voice_preview(self, body):
        """Synthesize a short sample so a voice can be auditioned before the full
        narration. Returns base64 mp3, like tts_studio."""
        voice = body.get("voice") or "en-US-AriaNeural"
        sample = (body.get("text") or "").strip() or (
            "साल 1969 था। तीन astronauts एक capsule में बैठे थे।"
            if voice.lower().startswith("hi")
            else "This is a quick preview of how this voice sounds.")

        async def _s():
            comm = edge_tts.Communicate(
                sample, voice, rate=body.get("rate") or "+0%",
                pitch=body.get("pitch") or "+0Hz",
                volume=body.get("volume") or "+0%")
            buf = bytearray()
            async for c in comm.stream():
                if c["type"] == "audio":
                    buf.extend(c["data"])
            return bytes(buf)

        try:
            mp3 = asyncio.run(_s())
        except Exception as e:
            return self._err(502, str(e))
        self._send(200, {"audio": base64.b64encode(mp3).decode()})

    def _run_stage(self, slug, stage, body):
        if not meta_path(slug).exists():
            return self._err(404, "no such project")
        d = pdir(slug)
        meta = read_meta(slug)

        if stage == "plan":
            context = (body.get("context") or "").strip()
            if not context:
                return self._err(400, "context required")
            seconds = int(body.get("seconds") or meta.get("seconds") or 60)
            guidance = body.get("guidance") or ""
            provider = body.get("provider") or "claude"
            if provider not in ("claude", "gemini", "claude-cli"):
                return self._err(400, "unknown provider")
            language = body.get("language") or "english"
            if language not in ("english", "hinglish"):
                return self._err(400, "unknown language")
            meta["seconds"] = seconds
            meta["script_provider"] = provider
            meta["language"] = language
            write_meta(slug, meta)
            (d / "context.txt").write_text(context)
            cmd = [PY, str(ROOT / "plan.py"),
                   "--context", str(d / "context.txt"),
                   "--seconds", str(seconds),
                   "--guidance", guidance,
                   "--language", language,
                   "--provider", provider,
                   "--out", str(d / "script.json")]

        elif stage == "narrate":
            if not (d / "script.json").exists():
                return self._err(400, "run Plan first")
            voice = body.get("voice") or "en-US-AriaNeural"
            meta["voice"] = voice
            write_meta(slug, meta)
            cmd = [PY, str(ROOT / "narrate.py"),
                   "--script", str(d / "script.json"),
                   "--voice", voice,
                   "--rate", body.get("rate") or "+0%",
                   "--pitch", body.get("pitch") or "+0Hz",
                   "--volume", body.get("volume") or "+0%",
                   "--out-mp3", str(d / "narration.mp3"),
                   "--out-words", str(d / "narration.words.json")]

        elif stage == "images":
            if not (d / "script.json").exists():
                return self._err(400, "run Plan first")
            cmd = [PY, str(ROOT / "generate_images.py"),
                   "--script", str(d / "script.json"),
                   "--out", str(d / "images"),
                   "--provider", meta.get("provider", "cloudflare")]

        elif stage == "metadata":
            if not (d / "script.json").exists():
                return self._err(400, "run Plan first")
            cmd = [PY, str(ROOT / "metadata.py"),
                   "--script", str(d / "script.json"),
                   "--provider", meta.get("script_provider", "claude-cli"),
                   "--out", str(d / "metadata.json")]

        elif stage == "assemble":
            miss = [n for n, ok in (("script", (d / "script.json").exists()),
                                    ("narration", (d / "narration.mp3").exists()),
                                    ("images", (d / "images").exists()))
                    if not ok]
            if miss:
                return self._err(400, f"missing: {', '.join(miss)}")
            if not FOOTAGE.exists():
                return self._err(400, f"no footage/ folder at {FOOTAGE}")
            cmd = [PY, str(ROOT / "assemble.py"),
                   "--script", str(d / "script.json"),
                   "--images", str(d / "images"),
                   "--audio", str(d / "narration.mp3"),
                   "--gameplay", str(FOOTAGE),
                   "--timings", str(d / "narration.words.json"),
                   "--out", str(d / "final.mp4")]
            seed = body.get("seed")
            if seed not in (None, ""):
                cmd += ["--seed", str(int(seed))]
            # narration.words.json carries edge-tts word timings for every voice
            # (English and Hindi alike), so assemble.py uses those precomputed
            # timings and needs neither whisperx nor a language special-case.
            # Caption font is auto-selected by assemble.py (Devanagari-aware).

            # gameplay: "" / "random" -> let assemble pick; else force that clip
            clip = (body.get("clip") or "").strip()
            if clip and clip.lower() != "random":
                cmd += ["--clip", clip]

            # music: "" / "none" -> none; "random" -> pick from library; else file
            music = (body.get("music") or "").strip()
            if music.lower() == "random":
                lib = media_files(MUSIC, {".mp3", ".m4a", ".wav"})
                music = random.choice(lib) if lib else ""
            if music and music.lower() != "none":
                track = MUSIC / music
                if track.exists():
                    cmd += ["--music", str(track)]
            meta["music"] = music or None
            meta["clip"] = clip or None
            write_meta(slug, meta)
        else:
            return self._err(404, "unknown stage")

        jid = start_job(stage, slug, cmd)
        self._send(200, {"job": jid})

    def _job(self, jid):
        with JOBS_LOCK:
            j = JOBS.get(jid)
            if not j:
                return self._err(404, "no such job")
            snap = dict(j)
        snap["tail"] = tail(snap["log"])
        self._send(200, snap)

    def _file(self, slug, rel):
        # confine to the project dir
        base = pdir(slug).resolve()
        target = (base / rel).resolve()
        if base not in target.parents and target != base:
            return self._err(403, "forbidden")
        if not target.exists() or not target.is_file():
            return self._err(404, "not found")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


# ---------------------------------------------------------------------- page

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Brain-rot Studio</title>
<style>
:root{
  --ink:#151110; --panel:#1E1917; --raised:#272120; --line:#392F2B;
  --amber:#E8A33D; --amber-dim:#8A6528; --cream:#EFE7DC; --dim:#8C7E73;
  --ok:#6FA86B; --bad:#D9694F; --run:#E8A33D;
  --mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,monospace;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ink);color:var(--cream);
  font:15px/1.55 system-ui,-apple-system,sans-serif}
a{color:var(--amber)}
.app{display:grid;grid-template-columns:260px 1fr;min-height:100vh}

/* rail */
.rail{background:var(--panel);border-right:1px solid var(--line);padding:20px 16px}
.rail h1{font-size:15px;font-weight:600;margin:0 0 2px}
.rail .sub{font:10px/1 var(--mono);color:var(--dim);letter-spacing:.1em;
  text-transform:uppercase;margin-bottom:20px;display:block}
.plist{list-style:none;padding:0;margin:0 0 16px}
.plist li{padding:9px 11px;border-radius:4px;cursor:pointer;font-size:14px;
  color:var(--dim);border:1px solid transparent}
.plist li:hover{color:var(--cream)}
.plist li.sel{background:var(--raised);color:var(--cream);border-color:var(--line)}
.newp{display:flex;flex-direction:column;gap:8px;border-top:1px solid var(--line);
  padding-top:16px}

/* main */
.main{padding:26px 30px 60px;max-width:900px}
.empty{color:var(--dim);margin-top:80px;text-align:center}
.phead{display:flex;align-items:baseline;gap:12px;margin-bottom:22px;
  border-bottom:1px solid var(--line);padding-bottom:14px}
.phead h2{margin:0;font-size:19px}
.phead .meta{font:11px var(--mono);color:var(--dim)}

/* stage card */
.card{background:var(--panel);border:1px solid var(--line);border-radius:6px;
  margin-bottom:16px;overflow:hidden}
.chead{display:flex;align-items:center;gap:12px;padding:14px 18px;cursor:pointer}
.chead .n{width:22px;height:22px;border-radius:50%;background:var(--raised);
  border:1px solid var(--line);display:grid;place-items:center;
  font:11px var(--mono);color:var(--dim)}
.chead h3{margin:0;font-size:15px;flex:1}
.badge{font:10px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;
  padding:4px 8px;border-radius:3px;border:1px solid var(--line);color:var(--dim)}
.badge.done{color:var(--ok);border-color:var(--ok)}
.badge.failed{color:var(--bad);border-color:var(--bad)}
.badge.running{color:var(--run);border-color:var(--run)}
.cbody{padding:0 18px 18px;display:none}
.card.open .cbody{display:block}
.card.open .chead .n{background:var(--amber);color:#1A1207;border-color:var(--amber)}

label{display:block;font:10px/1 var(--mono);color:var(--dim);
  letter-spacing:.09em;text-transform:uppercase;margin:14px 0 7px}
textarea,input[type=text],input[type=number],select{width:100%;background:var(--raised);
  color:var(--cream);border:1px solid var(--line);border-radius:3px;padding:10px 11px;
  font:14px system-ui,sans-serif}
textarea{min-height:120px;resize:vertical;font-family:var(--mono);font-size:13px}
select{font-family:var(--mono);font-size:13px}
.faders{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:6px}
.faders .fv{font:12px var(--mono);color:var(--amber)}
input[type=range]{-webkit-appearance:none;width:100%;height:20px;background:transparent}
input[type=range]::-webkit-slider-runnable-track{height:2px;background:var(--line)}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:11px;height:20px;
  margin-top:-9px;background:var(--amber);border-radius:2px}
.rowb{display:flex;gap:10px;align-items:center;margin-top:16px;flex-wrap:wrap}

button{background:var(--amber);color:#1A1207;border:0;border-radius:3px;
  padding:10px 20px;font:12px/1 var(--mono);font-weight:600;letter-spacing:.05em;
  text-transform:uppercase;cursor:pointer}
button.ghost{background:transparent;color:var(--dim);border:1px solid var(--line)}
button:disabled{opacity:.4;cursor:default}

pre.log{background:#100D0C;border:1px solid var(--line);border-radius:3px;
  padding:11px;margin-top:14px;font:11px/1.5 var(--mono);color:#B9A597;
  max-height:220px;overflow:auto;white-space:pre-wrap;display:none}
pre.log.show{display:block}
.preview{margin-top:14px}
.preview img{width:96px;height:96px;object-fit:cover;border-radius:3px;
  border:1px solid var(--line);margin:0 6px 6px 0}
audio,video{width:100%;margin-top:8px;border-radius:4px}
video{max-height:520px;background:#000}
.hint{font:11px/1.5 var(--mono);color:var(--dim);margin-top:8px}
.err{color:var(--bad)}
</style></head><body>
<div class="app">
  <aside class="rail">
    <h1>Brain-rot Studio</h1>
    <span class="sub">pipeline orchestrator</span>
    <ul class="plist" id="plist"></ul>
    <div class="newp">
      <label style="margin:0">New project</label>
      <input type="text" id="ntitle" placeholder="Working title">
      <input type="number" id="nsec" value="60" min="15" max="180" title="target seconds">
      <button id="ncreate">Create</button>
    </div>
  </aside>
  <main class="main" id="main">
    <div class="empty">Select a project, or create one to begin.</div>
  </main>
</div>
<script>
const $=s=>document.querySelector(s);
const api=(u,o)=>fetch(u,o).then(async r=>{const d=await r.json().catch(()=>({}));
  if(!r.ok)throw new Error(d.error||r.statusText);return d;});
let VOICES=[], CUR=null, POLL={};

const fmt=(v,u)=>(v>=0?'+':'')+v+u;

async function loadProjects(sel){
  const ps=await api('/api/projects');
  const ul=$('#plist'); ul.innerHTML='';
  ps.forEach(p=>{
    const li=document.createElement('li');
    li.textContent=p.title; li.dataset.slug=p.slug;
    if(p.slug===sel) li.classList.add('sel');
    li.onclick=()=>openProject(p.slug);
    ul.appendChild(li);
  });
}

$('#ncreate').onclick=async()=>{
  const title=$('#ntitle').value.trim();
  if(!title)return;
  const sec=+$('#nsec').value||60;
  const {slug}=await api('/api/projects',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({title,seconds:sec})});
  $('#ntitle').value='';
  await loadProjects(slug);
  openProject(slug);
};

function badge(st){const s=st?.status||'';return `<span class="badge ${s}">${s||'idle'}</span>`;}

async function openProject(slug){
  CUR=slug;
  document.querySelectorAll('.plist li').forEach(li=>
    li.classList.toggle('sel',li.dataset.slug===slug));
  const p=await api('/api/project/'+slug);
  render(p);
}

function stageCard(n,key,title,st,inner,openIt){
  return `<div class="card ${openIt?'open':''}" data-stage="${key}">
    <div class="chead" onclick="this.parentNode.classList.toggle('open')">
      <span class="n">${n}</span><h3>${title}</h3><span id="b_${key}">${badge(st)}</span>
    </div>
    <div class="cbody">${inner}
      <pre class="log" id="log_${key}"></pre>
    </div></div>`;
}

function render(p){
  const a=p.artifacts||{}, st=p.stages||{};
  const voiceOpts=VOICES.map(v=>`<option value="${v.name}" ${p.voice===v.name?'selected':''}>${v.name} · ${v.locale} · ${v.gender}${v.traits?' · '+v.traits:''}</option>`).join('');

  const plan=`
    <label>Story / article context</label>
    <textarea id="ctx" placeholder="Paste the raw story, article or notes...">${''}</textarea>
    <label>Narration guidance (optional — tone / POV / emphasis)</label>
    <input type="text" id="guid" placeholder="e.g. second person, ominous, emphasise the twist">
    <div class="rowb">
      <label style="margin:0">Language</label>
      <select id="lang" style="width:150px">
        <option value="english" ${(p.language||'english')==='english'?'selected':''}>English</option>
        <option value="hinglish" ${p.language==='hinglish'?'selected':''}>Hinglish</option>
      </select>
      <label style="margin:0">Model</label>
      <select id="prov" style="width:240px">
        <option value="claude-cli" ${(p.script_provider||'claude-cli')==='claude-cli'?'selected':''}>Claude Code · Opus 5 high (subscription)</option>
        <option value="claude" ${p.script_provider==='claude'?'selected':''}>Claude API · Sonnet 5</option>
        <option value="gemini" ${p.script_provider==='gemini'?'selected':''}>Gemini API · 2.0 Flash</option>
      </select>
      <label style="margin:0">Seconds</label>
      <input type="number" id="psec" value="${p.seconds||60}" min="15" max="180" style="width:90px">
      <button onclick="runStage('plan')">Generate script</button>
      ${a.script?'<button class="ghost" onclick="editScript()">View / edit script.json</button>':''}
    </div>
    <div id="scriptbox"></div>`;

  const narr=`
    <label>Voice</label>
    <select id="voice">${voiceOpts||'<option>loading…</option>'}</select>
    <div class="faders">
      <div><label>Rate <span class="fv" id="rv">+0%</span></label>
        <input type="range" id="rate" min="-50" max="50" value="0"></div>
      <div><label>Pitch <span class="fv" id="pv">+0Hz</span></label>
        <input type="range" id="pitch" min="-50" max="50" value="0"></div>
      <div><label>Volume <span class="fv" id="vv">+0%</span></label>
        <input type="range" id="vol" min="-50" max="50" value="0"></div>
    </div>
    <div class="rowb">
      <button onclick="runStage('narrate')">Synthesize narration</button>
      <button class="ghost" onclick="previewVoice()">▶ Preview voice</button>
      <audio id="vprev" style="display:none;margin:0;width:auto"></audio>
    </div>
    <p class="hint" id="vpmsg"></p>
    ${a.narration?`<div class="preview"><audio controls src="/file/${p.slug}/narration.mp3?t=${Date.now()}"></audio></div>`:''}`;

  const imgs=`
    <p class="hint">Provider: ${p.provider||'cloudflare'} (FLUX.1 Schnell, watermark-free). One image per visual.</p>
    <div class="rowb"><button onclick="runStage('images')" ${a.script?'':'disabled'}>Generate images</button>
      <span class="hint">${a.images?a.images+' image(s) on disk':'no images yet'}</span></div>
    <div class="preview" id="imgrid"></div>`;

  const metad=`
    <p class="hint">Title, description, hashtags & tags — in the project's language, via the script model.</p>
    <div class="rowb">
      <button onclick="runStage('metadata')" ${a.script?'':'disabled'}>Generate metadata</button>
      ${a.metadata?'<button class="ghost" onclick="editMeta()">View / edit</button>':''}
    </div>
    <div id="metabox"></div>`;

  const asm=`
    <div class="rowb">
      <label style="margin:0">Gameplay</label>
      <select id="clip" style="width:220px"><option value="random">Random</option></select>
      <label style="margin:0">Music</label>
      <select id="music" style="width:200px"><option value="none">None</option><option value="random">Random</option></select>
    </div>
    <div class="rowb">
      <label style="margin:0">Seed</label>
      <input type="number" id="seed" placeholder="random" style="width:110px">
      <button onclick="runStage('assemble')" ${(a.script&&a.narration&&a.images)?'':'disabled'}>Assemble final.mp4</button>
    </div>
    <p class="hint">Captions use edge-tts word timings (word-synced); font auto-switches for Hinglish.</p>
    ${a.final?`<div class="preview"><video controls src="/file/${p.slug}/final.mp4?t=${Date.now()}"></video></div>`:''}`;

  $('#main').innerHTML=`
    <div class="phead"><h2>${p.title}</h2>
      <span class="meta">${p.slug} · ${p.seconds||60}s · ${p.language||'english'}</span></div>
    ${stageCard(1,'plan','Plan — script.json',st.plan,plan,!a.script)}
    ${stageCard(2,'narrate','Narrate — narration.mp3',st.narrate,narr,a.script&&!a.narration)}
    ${stageCard(3,'images','Images',st.images,imgs,a.script&&!a.images)}
    ${stageCard(4,'metadata','Metadata — title / tags',st.metadata,metad,a.script&&a.images&&!a.metadata)}
    ${stageCard(5,'assemble','Assemble — final.mp4',st.assemble,asm,a.script&&a.narration&&a.images&&!a.final)}`;

  // wire faders
  const bind=(sl,out,u)=>{const e=$('#'+sl);if(e)e.oninput=()=>$('#'+out).textContent=fmt(+e.value,u);};
  bind('rate','rv','%'); bind('pitch','pv','Hz'); bind('vol','vv','%');
  // Hinglish projects with no chosen voice default to a Hindi voice
  if(!p.voice && p.language==='hinglish'){const vs=$('#voice');
    if(vs&&[...vs.options].some(o=>o.value==='hi-IN-MadhurNeural'))vs.value='hi-IN-MadhurNeural';}
  if(a.images) loadImages(p.slug,a.images);
  populateMedia(p);
  if(a.metadata) editMeta();
  // resume any running stage log
  Object.entries(st).forEach(([k,v])=>{if(v.status==='running')pollStageLog(k);});
}

async function populateMedia(p){
  const clip=$('#clip'), music=$('#music');
  if(clip){const gs=await api('/api/gameplay').catch(()=>[]);
    gs.forEach(f=>{const o=document.createElement('option');o.value=f;o.textContent=f.slice(0,40);
      if(p.clip===f)o.selected=true;clip.appendChild(o);});}
  if(music){const ms=await api('/api/music').catch(()=>[]);
    ms.filter(f=>!f.endsWith('.md')).forEach(f=>{const o=document.createElement('option');o.value=f;o.textContent=f.slice(0,40);
      if(p.music===f)o.selected=true;music.appendChild(o);});}
}

async function previewVoice(){
  const msg=$('#vpmsg'); msg.className='hint'; msg.textContent='rendering preview…';
  try{
    const d=await api('/api/voice-preview',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({voice:$('#voice').value,
        rate:fmt(+$('#rate').value,'%'),pitch:fmt(+$('#pitch').value,'Hz'),
        volume:fmt(+$('#vol').value,'%')})});
    const a=$('#vprev'); a.src='data:audio/mpeg;base64,'+d.audio; a.play(); msg.textContent='';
  }catch(e){msg.className='hint err';msg.textContent=e.message;}
}

async function editMeta(){
  const box=$('#metabox');
  const m=await fetch('/file/'+CUR+'/metadata.json').then(r=>r.json()).catch(()=>null);
  if(!m){box.innerHTML='';return;}
  box.innerHTML=`
    <label>Title</label><input type="text" id="m_title" value="${(m.title||'').replace(/"/g,'&quot;')}">
    <label>Description</label><textarea id="m_desc" style="min-height:100px">${(m.description||'').replace(/</g,'&lt;')}</textarea>
    <label>Hashtags (space-separated)</label><input type="text" id="m_tags" value="${(m.hashtags||[]).join(' ')}">
    <label>SEO tags (comma-separated)</label><input type="text" id="m_seo" value="${(m.tags||[]).join(', ')}">
    <div class="rowb"><button onclick="saveMeta()">Save</button><span class="hint" id="msave">category: ${m.category||''}</span></div>`;
  box._cat=m.category;
}
async function saveMeta(){
  const data={title:$('#m_title').value,description:$('#m_desc').value,
    hashtags:$('#m_tags').value.split(/\s+/).filter(Boolean),
    tags:$('#m_seo').value.split(',').map(s=>s.trim()).filter(Boolean),
    category:$('#metabox')._cat||'People & Blogs'};
  try{await api('/api/project/'+CUR+'/metadata',{method:'PUT',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({data})});
    $('#msave').textContent='saved ✓';
  }catch(e){$('#msave').innerHTML='<span class="err">'+e.message+'</span>';}
}

async function loadImages(slug,count){
  const grid=$('#imgrid'); if(!grid)return; grid.innerHTML='';
  // pull the image filenames straight from script.json
  const files=[];
  const script=await fetch('/file/'+slug+'/script.json').then(r=>r.json()).catch(()=>null);
  if(script){script.beats.forEach(b=>b.visuals.forEach(v=>{if(v.image)files.push(v.image);}));}
  [...new Set(files)].forEach(fn=>{
    const im=document.createElement('img');
    im.src='/file/'+slug+'/images/'+fn+'?t='+Date.now();
    im.onerror=()=>im.remove();
    grid.appendChild(im);
  });
}

async function runStage(stage){
  const body={};
  if(stage==='plan'){body.context=$('#ctx').value;body.guidance=$('#guid').value;body.seconds=+$('#psec').value;body.provider=$('#prov').value;body.language=$('#lang').value;}
  if(stage==='narrate'){body.voice=$('#voice').value;
    body.rate=fmt(+$('#rate').value,'%');body.pitch=fmt(+$('#pitch').value,'Hz');
    body.volume=fmt(+$('#vol').value,'%');}
  if(stage==='assemble'){const s=$('#seed').value;if(s!=='')body.seed=+s;
    body.clip=$('#clip').value;body.music=$('#music').value;}
  const log=$('#log_'+stage); log.classList.add('show'); log.textContent='starting…';
  $('#b_'+stage).innerHTML=badge({status:'running'});
  try{
    const {job}=await api('/api/project/'+CUR+'/'+stage,{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    pollJob(job,stage);
  }catch(e){log.textContent='error: '+e.message;$('#b_'+stage).innerHTML=badge({status:'failed'});}
}

function pollJob(job,stage){
  clearInterval(POLL[stage]);
  POLL[stage]=setInterval(async()=>{
    let j; try{j=await api('/api/jobs/'+job);}catch(e){return;}
    const log=$('#log_'+stage); log.textContent=j.tail||''; log.scrollTop=log.scrollHeight;
    $('#b_'+stage).innerHTML=badge({status:j.status});
    if(j.status!=='running'){clearInterval(POLL[stage]);
      if(j.status==='done') openProject(CUR);}
  },1500);
}

function pollStageLog(stage){/* on reload, we don't have the job id; refresh badge via project */}

async function editScript(){
  const box=$('#scriptbox');
  const txt=await fetch('/file/'+CUR+'/script.json').then(r=>r.text());
  box.innerHTML=`<label>script.json</label><textarea id="scr" style="min-height:280px">${txt.replace(/</g,'&lt;')}</textarea>
    <div class="rowb"><button onclick="saveScript()">Save</button>
    <span class="hint" id="ssave"></span></div>`;
}
async function saveScript(){
  try{
    await api('/api/project/'+CUR+'/script',{method:'PUT',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({script:$('#scr').value})});
    $('#ssave').textContent='saved ✓';
  }catch(e){$('#ssave').innerHTML='<span class="err">'+e.message+'</span>';}
}

(async()=>{
  try{VOICES=await api('/api/voices');}catch(e){}
  await loadProjects();
})();
</script></body></html>"""


def main():
    load_env()
    PROJECTS.mkdir(exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"Brain-rot Studio  ->  {url}   (ctrl-c to stop)")
    if not (ROOT / ".env").exists():
        print("note: no .env found - add ANTHROPIC_API_KEY, CF_ACCOUNT_ID, CF_API_TOKEN")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        srv.shutdown()


if __name__ == "__main__":
    main()
