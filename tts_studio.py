#!/usr/bin/env python3
"""
tts_studio.py - local UI for tuning edge-tts narration.

    pip install edge-tts
    python tts_studio.py

Opens http://localhost:7860. Paste text, drag the faders, hit Render.
No dependencies beyond edge-tts - the server is stdlib http.server.

Word timings come back with every render (boundary="WordBoundary"), which is
the same data assemble.py needs for caption alignment.
"""

import asyncio
import base64
import json
import subprocess
import sys
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import edge_tts

PORT = 7860

SAMPLE = ("साल 1969 था। तीन astronauts एक metal capsule में बैठे थे। "
          "Capsule 24,000 miles per hour की speed से ocean में टकराया। "
          "Heat shield जलकर एक तिहाई रह गई। तीन minute तक radio contact टूट गया।")


# ---------------------------------------------------------------- synthesis

async def _voices():
    vs = await edge_tts.list_voices()
    return sorted(
        ({"name": v["ShortName"], "locale": v["Locale"], "gender": v["Gender"],
          "traits": ", ".join(v.get("VoicePersonalities") or [])} for v in vs),
        key=lambda v: (v["locale"] != "hi-IN", v["locale"] != "en-IN",
                       v["locale"], v["name"]))


async def _synth(text, voice, rate, volume, pitch):
    comm = edge_tts.Communicate(text, voice, rate=rate, volume=volume,
                                pitch=pitch, boundary="WordBoundary")
    audio, words = bytearray(), []
    async for c in comm.stream():
        if c["type"] == "audio":
            audio.extend(c["data"])
        elif c["type"] == "WordBoundary":
            words.append({"text": c["text"],
                          "start": round(c["offset"] / 1e7, 3),
                          "end": round((c["offset"] + c["duration"]) / 1e7, 3)})
    return bytes(audio), words


def _duration(mp3: bytes, words) -> float:
    """ffprobe if present, else fall back to the last word boundary."""
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(mp3)
            p = f.name
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", p],
            capture_output=True, text=True, timeout=15)
        Path(p).unlink(missing_ok=True)
        if r.returncode == 0:
            return float(r.stdout.strip())
    except Exception:
        pass
    return words[-1]["end"] if words else 0.0


# ------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif self.path == "/api/voices":
            try:
                self._send(200, json.dumps(asyncio.run(_voices())))
            except Exception as e:
                self._send(502, json.dumps({"error": str(e)}))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if self.path != "/api/synth":
            return self._send(404, json.dumps({"error": "not found"}))

        n = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, json.dumps({"error": "bad JSON"}))

        text = (req.get("text") or "").strip()
        if not text:
            return self._send(400, json.dumps({"error": "No text to render."}))

        try:
            mp3, words = asyncio.run(_synth(
                text, req.get("voice", "hi-IN-MadhurNeural"),
                req.get("rate", "+0%"), req.get("volume", "+0%"),
                req.get("pitch", "+0Hz")))
        except Exception as e:
            return self._send(502, json.dumps({"error": str(e)}))

        secs = _duration(mp3, words)
        wc = len(text.split())
        self._send(200, json.dumps({
            "audio": base64.b64encode(mp3).decode(),
            "words": words,
            "seconds": round(secs, 2),
            "word_count": wc,
            "wpm": round(wc / (secs / 60), 1) if secs else 0,
        }))


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TTS Studio</title>
<style>
:root{
  --ink:#151110; --panel:#1E1917; --raised:#272120; --line:#392F2B;
  --amber:#E8A33D; --amber-dim:#8A6528; --cream:#EFE7DC; --dim:#8C7E73;
  --mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,monospace;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ink);color:var(--cream);
  font:15px/1.55 system-ui,-apple-system,sans-serif;
  padding:28px 20px 60px}
.wrap{max-width:940px;margin:0 auto}

header{display:flex;align-items:baseline;gap:14px;
  border-bottom:1px solid var(--line);padding-bottom:14px;margin-bottom:22px}
h1{font-size:17px;font-weight:600;letter-spacing:-.01em;margin:0}
.sub{font:11px/1 var(--mono);color:var(--dim);letter-spacing:.08em;
  text-transform:uppercase}

label{display:block;font:11px/1 var(--mono);color:var(--dim);
  letter-spacing:.09em;text-transform:uppercase;margin-bottom:8px}

textarea{width:100%;min-height:132px;background:var(--panel);
  color:var(--cream);border:1px solid var(--line);border-radius:3px;
  padding:14px;font:15px/1.65 system-ui,sans-serif;resize:vertical}
textarea:focus,select:focus,button:focus-visible,input:focus-visible{
  outline:2px solid var(--amber);outline-offset:2px}

.row{display:grid;grid-template-columns:1fr auto;gap:18px;
  align-items:end;margin:18px 0}
select{background:var(--panel);color:var(--cream);border:1px solid var(--line);
  border-radius:3px;padding:9px 11px;font:13px var(--mono);width:100%}

/* channel strip */
.strip{display:grid;grid-template-columns:repeat(3,1fr);gap:0;
  background:var(--panel);border:1px solid var(--line);border-radius:3px;
  margin:18px 0}
.fader{padding:16px 18px 18px;border-right:1px solid var(--line)}
.fader:last-child{border-right:0}
.fhead{display:flex;justify-content:space-between;align-items:baseline;
  margin-bottom:12px}
.fname{font:11px/1 var(--mono);color:var(--dim);letter-spacing:.09em;
  text-transform:uppercase}
.fval{font:15px/1 var(--mono);color:var(--amber);font-variant-numeric:tabular-nums}

input[type=range]{-webkit-appearance:none;appearance:none;width:100%;
  height:22px;background:transparent;cursor:pointer;margin:0}
input[type=range]::-webkit-slider-runnable-track{height:2px;
  background:var(--line)}
input[type=range]::-moz-range-track{height:2px;background:var(--line)}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;
  width:12px;height:22px;margin-top:-10px;background:var(--amber);
  border-radius:2px}
input[type=range]::-moz-range-thumb{width:12px;height:22px;border:0;
  background:var(--amber);border-radius:2px}
.ticks{display:flex;justify-content:space-between;
  font:10px var(--mono);color:#5B4F48;margin-top:3px}

/* presets */
.presets{display:flex;gap:8px;align-items:center;margin:14px 0 20px;
  flex-wrap:wrap}
.presets span{font:11px var(--mono);color:var(--dim);letter-spacing:.09em;
  text-transform:uppercase;margin-right:4px}
.chip{background:transparent;color:var(--dim);border:1px solid var(--line);
  border-radius:3px;padding:6px 12px;font:12px var(--mono);cursor:pointer}
.chip:hover{color:var(--amber);border-color:var(--amber-dim)}

button.go{background:var(--amber);color:#1A1207;border:0;border-radius:3px;
  padding:11px 26px;font:13px/1 var(--mono);font-weight:600;
  letter-spacing:.06em;text-transform:uppercase;cursor:pointer;
  white-space:nowrap}
button.go:disabled{opacity:.4;cursor:default}

/* budget meter - the readout that matters */
.meter{background:var(--panel);border:1px solid var(--line);border-radius:3px;
  padding:16px 18px;margin:20px 0}
.mtop{display:flex;justify-content:space-between;align-items:baseline;
  margin-bottom:12px}
.mstats{display:flex;gap:22px;font:12px var(--mono);color:var(--dim)}
.mstats b{color:var(--cream);font-weight:400;font-size:14px}
.track{position:relative;height:26px;background:var(--raised);
  border-radius:2px;overflow:hidden}
.fill{position:absolute;inset:0 auto 0 0;width:0;background:var(--amber-dim);
  transition:width .28s cubic-bezier(.2,.7,.3,1)}
.fill.over{background:#8C3A2A}
.scale{display:flex;justify-content:space-between;font:10px var(--mono);
  color:#5B4F48;margin-top:5px}

audio{width:100%;margin-top:16px;height:38px}
.msg{font:12px/1.5 var(--mono);color:var(--dim);margin-top:12px}
.msg.err{color:#D9694F}

details{margin-top:20px;border-top:1px solid var(--line);padding-top:16px}
summary{font:11px var(--mono);color:var(--dim);letter-spacing:.09em;
  text-transform:uppercase;cursor:pointer}
pre{background:var(--panel);border:1px solid var(--line);border-radius:3px;
  padding:13px;overflow:auto;font:12px/1.55 var(--mono);color:var(--dim);
  max-height:260px;margin-top:12px}

@media(max-width:660px){
  .strip{grid-template-columns:1fr}
  .fader{border-right:0;border-bottom:1px solid var(--line)}
  .fader:last-child{border-bottom:0}
  .row{grid-template-columns:1fr}
}
@media(prefers-reduced-motion:reduce){.fill{transition:none}}
</style></head><body>
<div class="wrap">

<header>
  <h1>TTS Studio</h1>
  <span class="sub">edge-tts &middot; prosody bench</span>
</header>

<label for="text">Narration</label>
<textarea id="text" spellcheck="false">__SAMPLE__</textarea>

<div class="row">
  <div>
    <label for="voice">Voice</label>
    <select id="voice"><option>loading…</option></select>
  </div>
  <button class="go" id="go">Render</button>
</div>

<div class="strip">
  <div class="fader">
    <div class="fhead"><span class="fname">Rate</span>
      <span class="fval" id="rateV">+0%</span></div>
    <input type="range" id="rate" min="-50" max="50" value="0" step="1">
    <div class="ticks"><span>−50</span><span>0</span><span>+50</span></div>
  </div>
  <div class="fader">
    <div class="fhead"><span class="fname">Pitch</span>
      <span class="fval" id="pitchV">+0Hz</span></div>
    <input type="range" id="pitch" min="-50" max="50" value="0" step="1">
    <div class="ticks"><span>−50</span><span>0</span><span>+50</span></div>
  </div>
  <div class="fader">
    <div class="fhead"><span class="fname">Volume</span>
      <span class="fval" id="volV">+0%</span></div>
    <input type="range" id="vol" min="-50" max="50" value="0" step="1">
    <div class="ticks"><span>−50</span><span>0</span><span>+50</span></div>
  </div>
</div>

<div class="presets">
  <span>Presets</span>
  <button class="chip" data-p="12,15,0">Hook</button>
  <button class="chip" data-p="5,0,0">Body</button>
  <button class="chip" data-p="-5,-10,0">Closing</button>
  <button class="chip" data-p="0,0,0">Flat</button>
</div>

<div class="meter">
  <div class="mtop">
    <div class="mstats">
      <span>len <b id="sSec">—</b></span>
      <span>words <b id="sWords">—</b></span>
      <span>wpm <b id="sWpm">—</b></span>
    </div>
    <span class="sub" id="verdict"></span>
  </div>
  <div class="track">
    <div class="fill" id="fill"></div>
  </div>
  <div class="scale"><span>0</span><span>30s</span><span>60s</span></div>
  <audio id="player" controls preload="none"></audio>
  <div class="msg" id="msg"></div>
</div>

<details>
  <summary>Word timings</summary>
  <pre id="timings">Render to see word-level boundaries.</pre>
</details>

</div>
<script>
const $ = id => document.getElementById(id);
const F = {rate:['rate','rateV','%'], pitch:['pitch','pitchV','Hz'],
           vol:['vol','volV','%']};
const fmt = (v,u) => (v>=0?'+':'') + v + u;

for (const [k,[sl,out,u]] of Object.entries(F))
  $(sl).addEventListener('input', () => $(out).textContent = fmt(+$(sl).value,u));

document.querySelectorAll('.chip').forEach(c =>
  c.addEventListener('click', () => {
    const [r,p,v] = c.dataset.p.split(',').map(Number);
    $('rate').value=r; $('pitch').value=p; $('vol').value=v;
    $('rateV').textContent=fmt(r,'%');
    $('pitchV').textContent=fmt(p,'Hz');
    $('volV').textContent=fmt(v,'%');
  }));

fetch('/api/voices').then(r=>r.json()).then(vs => {
  const sel = $('voice'); sel.innerHTML='';
  const indian = vs.filter(v=>['hi-IN','en-IN'].includes(v.locale));
  const mk = (g,list) => {
    const og = document.createElement('optgroup'); og.label = g;
    list.forEach(v => {
      const o = document.createElement('option');
      o.value = v.name;
      o.textContent = v.name + ' · ' + v.gender + (v.traits ? ' · '+v.traits : '');
      og.appendChild(o);
    });
    sel.appendChild(og);
  };
  if (indian.length) mk('Indian locales', indian);
  mk('All voices', vs);
  if (indian.length) sel.value = indian[0].name;
}).catch(e => $('msg').textContent = 'Could not load voices: ' + e.message);

$('go').addEventListener('click', async () => {
  const btn = $('go'), msg = $('msg');
  btn.disabled = true; btn.textContent = 'Rendering';
  msg.className='msg'; msg.textContent='';
  try {
    const r = await fetch('/api/synth', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        text: $('text').value,
        voice: $('voice').value,
        rate: fmt(+$('rate').value,'%'),
        pitch: fmt(+$('pitch').value,'Hz'),
        volume: fmt(+$('vol').value,'%')
      })});
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'render failed');

    $('player').src = 'data:audio/mpeg;base64,' + d.audio;
    $('sSec').textContent = d.seconds.toFixed(1)+'s';
    $('sWords').textContent = d.word_count;
    $('sWpm').textContent = d.wpm;

    const pct = Math.min(d.seconds/60*100, 100);
    const fill = $('fill');
    fill.style.width = pct+'%';
    fill.classList.toggle('over', d.seconds > 60);

    const diff = d.seconds - 60;
    $('verdict').textContent = Math.abs(diff) < 2 ? 'on target'
      : diff > 0 ? `${diff.toFixed(1)}s over` : `${(-diff).toFixed(1)}s under`;

    const budget = Math.round(d.wpm);
    msg.textContent = `At this rate a 60s script needs ~${budget} words.`;
    $('timings').textContent = d.words.length
      ? JSON.stringify(d.words, null, 1)
      : 'No boundaries returned.';
  } catch(e) {
    msg.className='msg err'; msg.textContent = e.message;
  } finally {
    btn.disabled = false; btn.textContent = 'Render';
  }
});
</script></body></html>""".replace("__SAMPLE__", SAMPLE)


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"TTS Studio  →  {url}   (ctrl-c to stop)")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        srv.shutdown()


if __name__ == "__main__":
    main()
