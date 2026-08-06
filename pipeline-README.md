# Steps 4-6: assemble.py

Turns `script.json` + images + `narration.mp3` + a gameplay folder into a
finished 1080x1920 short. Images on top, gameplay on the bottom, karaoke
captions centred.

## Install

    pip install whisperx        # optional but strongly recommended

**ffmpeg must have libass.** Captions are burned with the `ass` filter, and the
stock Homebrew `ffmpeg` bottle is built WITHOUT libass. On macOS install the
full build:

    brew install ffmpeg-full    # keg-only; has libass, freetype, fontconfig

`assemble.py` auto-discovers a libass build (it prefers `ffmpeg-full` even though
it is keg-only, so no PATH change is needed). Override with
`FFMPEG_BIN=/path/to/ffmpeg`. If no libass build is found it exits immediately
with instructions, rather than rendering every shot and then failing on the
caption step.

## Run

    python assemble.py \
      --script   out/script.json \
      --images   out/images \
      --audio    out/narration.mp3 \
      --gameplay footage/ \
      --out      final.mp4 \
      --seed     7

`--music music.mp3` is optional; omit it for narration-only audio.

On the first whisperx run torchaudio downloads a ~360 MB alignment model to
`~/.cache/torch/hub/checkpoints/`. If it stalls (hangs at 0% CPU on a partial
`.pth`), fetch it directly and rerun:

    curl -L -C - -o ~/.cache/torch/hub/checkpoints/wav2vec2_fairseq_base_ls960_asr_ls960.pth \
      https://download.pytorch.org/torchaudio/models/wav2vec2_fairseq_base_ls960_asr_ls960.pth

## Preflight checks

Before rendering, `assemble.py` verifies every visual has an `image` that exists
on disk and warns if `narration` does not equal the beat texts joined (that
mismatch silently drifts caption timing). Both fail fast, not mid-render.

Flags: `--no-align` skips whisperx (words spread proportionally, captions
approximate), `--keep` retains the temp work dir for inspecting individual shots.

## script.json contract

Each visual needs an `image` filename (relative to `--images`), a `motion`,
and a planned `seconds`. Multiple visuals may point at the SAME image file
with different motions - that is how 6 images become 12+ shots.

    {
      "narration": "full text, must exactly equal all beat texts joined",
      "beats": [
        {"id": 1, "text": "beat narration",
         "visuals": [
           {"image": "img_01.png", "motion": "slow push in",  "seconds": 4.0},
           {"image": "img_01.png", "motion": "drift right",   "seconds": 3.5}
         ]}
      ]
    }

`seconds` is only a WEIGHT. Real durations come from alignment; the weights
just decide how a beat's measured time is split across its shots.

Motions: `slow push in`, `slow pull out`, `drift left`, `drift right`,
`tilt up`, `tilt down`.

## How sync works

The narration audio is the master clock.

1. Forced alignment gives word-level timestamps for the known narration text.
2. Words are counted off against beat texts to get each beat's real span.
3. Each shot is rendered to its exact share of that span.
4. Concatenated shots therefore equal the narration length automatically.

Measured drift on the test run: 38ms. No manual sync anywhere.

## Constants worth tuning

    PANEL_H = 960      # split point; raise for a taller image panel
    SUPERSAMPLE = 2    # stills rendered at 2x before zoompan
    MUSIC_GAIN = 0.12  # music level under narration
    zoom range 1.0 -> 1.14 in zoompan_filter()

## Gotchas already handled

- zoompan `d` is in FRAMES not seconds
- stills are cover-cropped to the panel aspect before zooming
- `-stream_loop -1` covers gameplay shorter than the narration
- gameplay audio is stripped with `-an`
- ASS path is escaped for libass
- filter labels do not collide between video and audio branches
