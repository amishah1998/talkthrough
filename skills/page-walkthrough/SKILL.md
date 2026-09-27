---
name: page-walkthrough
description: Turn an existing HTML page or PDF (a report, recap, cheat sheet, slide deck, docs page) into a narrated walkthrough video of the page itself, like an automatic screen-share. It captures the real page, moves a camera over one section at a time with a spotlight, adds a voiceover written only from the page's own words, and burns in captions synced to the voice. Nothing is redrawn or invented. Use when the user says "make a video of this page", "walkthrough video", "turn this HTML/PDF into a video", "narrate this report", "screen-share video of this", "video version of this doc", "/page-walkthrough". Not for motion graphics designed from scratch, and not for screen recordings of an app in use.
---

# Page walkthrough

The video shows the page the reader already has, section by section, with a voice explaining it.
The promise is fidelity: every frame is a real crop of the real page, and every sentence of
narration is something the page says. That is what makes it safe for reports, recaps and anything
with numbers in it.

The `page-walkthrough` CLI does the mechanics. Your job is the part that needs judgement: deciding
what on the page matters, and writing what the voice says over each part.

## Setup

Run `page-walkthrough doctor`. If the command is missing, use
`uvx --from git+https://github.com/karanb192/page-walkthrough page-walkthrough doctor`, or install it
with `pip install "git+https://github.com/karanb192/page-walkthrough"`. It needs a Chromium browser
(Chrome, Chromium or Edge) and ffmpeg. For the voice it uses, in order: ElevenLabs if
`ELEVENLABS_API_KEY` is set, OpenAI if `OPENAI_API_KEY` is set, the local Kokoro voice if installed
(`pip install "page-walkthrough[local]"`), then macOS `say`.

## Workflow

Use a fresh work folder per video, e.g. `./walkthrough-work/<name>`.

1. **Capture.** `page-walkthrough capture PAGE --out DIR`
   PAGE is an .html file, a URL or a .pdf. It writes `page.png` (the whole page, or every PDF page
   stacked), `boxes.json` (each section with an id, its rect in css px, and a text excerpt),
   `page.txt` and `page.json`. HTML sections come from the DOM; PDF sections come from the white
   space between blocks, so scanned PDFs work too.

2. **Decide what matters.** Read `page.txt` in full. Look at `page.png` in slices if the layout is
   not obvious from the text. Pick the sections a busy reader most needs, following the page's own
   signals: its title and opening promise, anything it calls key or essential, the central diagram
   or table, the decisions, the takeaways, the closing call to action. Skip what is meant to be
   looked up rather than heard: long reference tables, appendices, source lists, boilerplate.
   If the user said what to focus on, that wins.

3. **Write `DIR/shots.json`.**

   ```json
   {
     "format": "portrait",
     "tts": "auto",
     "shots": [
       {"boxes": ["b0"], "say": "The page's hook, in one or two sentences."},
       {"boxes": ["b41", "b42"], "say": "One idea, framed by two boxes together."},
       {"rect": [95, 828, 470, 440], "move": "hold", "say": "Part of a wide diagram, in css px."}
     ]
   }
   ```

   - `format`: `portrait` 1080x1920 (default, for phones), `landscape` 1920x1080, `square` 1080x1080.
     A single-column page reads best in portrait.
   - `boxes` frames the union of those boxes; `rect` frames a hand-picked region instead. The camera
     will not zoom past about 1.15x the capture's pixel density, so text stays sharp. Split a wide
     diagram into two or three `rect` shots so a phone can read it.
   - `move`: `auto` (default) pans down a tall region and slowly pushes in on the rest; `pan`,
     `zoom` and `hold` force one.
   - `spotlight` (default on) dims everything outside the framed region and outlines it, so the
     viewer knows what the voice is talking about. Set `false` per shot or at the top level.
   - `tts`: `auto`, `elevenlabs`, `openai`, `kokoro` or `say`. With a named backend you may also set
     `voice`, `model` (cloud), `instructions` (OpenAI), `speed` (Kokoro) or `rate` (say).

   If the user has no agent handy, `page-walkthrough plan DIR --seconds 90` asks Claude to write this
   file (needs `pip install "page-walkthrough[plan]"` and `ANTHROPIC_API_KEY`).

4. **Preview before rendering.** `page-walkthrough preview DIR` writes `DIR/preview.png`, one
   spotlighted frame per shot, and prints the expected length. Look at it. Each shot must frame what
   its narration talks about, with no key text cut at the edge. Fix `shots.json` and preview again.

5. **Render.** `page-walkthrough render DIR --out NAME.mp4`. It voices each shot, moves the camera
   with eased travel between shots, draws captions (synced to the voice's word timings when the
   backend gives them), adds a progress line, and encodes H.264 + AAC at 30 fps. Expect roughly
   real-time rendering: a 90-second video takes about 90 seconds on a laptop.

6. **Check the real video.** Pull a few frames with `ffmpeg -ss T -i NAME.mp4 -frames:v 1 f.png`,
   including one mid-transition and one on the densest shot, and look at them before handing over.

## Writing the narration

- **Only what the page says.** Paraphrase for the ear, but add no fact, number, name, claim or
  example that is not on the page. If the page hedges, the voice hedges. An invented line breaks the
  one promise this format makes.
- **60 to 120 seconds**, about 150 to 300 words. Each shot 1 to 3 short sentences, 12 to 45 words.
  More shots with less text beat a few long monologues.
- **Page order, with a spine.** Open on the hook, walk the sections that matter, close on the
  page's own takeaway or next step.
- **Point at what is on screen.** "This diagram", "the bad one on the left", "step three".
- **Write for a voice.** Spell out symbols it would stumble on ("server dot py"), drop code syntax
  and parentheses, and use plain punctuation.

## Gotchas

- **Box ids are positional.** Re-capturing after the page changes can shift every id; write
  `shots.json` against the capture you render from.
- **A frame is usually taller than the section it shows.** The spotlight handles that; do not
  shrink `rect` below the zoom limit to crop neighbours away.
- **Pages that need a login or build their content slowly** may capture half-loaded. Capture a saved
  copy or the PDF instead.
- **Caption timing without word timings** (OpenAI, `say`) is spread by word count within each shot;
  it tracks well at a steady pace.
