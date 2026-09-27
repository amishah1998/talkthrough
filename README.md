# page-walkthrough

**Any HTML page or PDF becomes a narrated walkthrough video of itself.**

A camera moves over the real page one section at a time, a spotlight shows what is being explained,
a voice explains it using only what the page says, and captions follow the voice. Nothing is
redrawn, restyled or invented: every frame is a real crop of your page, so the diagram in the video
is the diagram in the document.

<!-- demo video goes here -->

Other tools regenerate your content: they write a fresh script and invent their own visuals. This
one keeps it. Use it for the report nobody will open, the recap your students watch on the bus, or
the docs page you want to post as a 90-second clip.

## Quickstart

```bash
pip install "git+https://github.com/karanb192/page-walkthrough"
page-walkthrough doctor                      # checks browser, ffmpeg and a voice
page-walkthrough make report.pdf --seconds 90
```

`make` captures the page, has Claude pick what matters and write the narration, shows you a preview
sheet, and renders `report-walkthrough.mp4`. It needs `ANTHROPIC_API_KEY` for the planning step
(`pip install "page-walkthrough[plan]"`).

### In Claude Code

```
/plugin marketplace add karanb192/page-walkthrough
/plugin install page-walkthrough@page-walkthrough
```

Then ask: "make a walkthrough video of recap.html". Claude reads the page, decides what to focus on,
writes the shot list, checks the framing on a preview sheet, and renders. No Anthropic API key
needed; the agent does the planning.

The skill in `skills/page-walkthrough/` follows the Agent Skills format, so other agents that read
skills can use the same folder.

## How it works

```
page.html / report.pdf
   │  capture   headless browser (HTML) or pdfium (PDF): one tall image + a list of sections
   ▼
page.png + boxes.json
   │  plan      an agent or Claude picks the sections that matter and writes what to say
   ▼
shots.json
   │  preview   one spotlighted frame per shot, to check framing before rendering
   │  render    voice per shot, eased camera moves, spotlight, captions, H.264 + AAC
   ▼
walkthrough.mp4   (portrait 1080x1920 by default; landscape and square too)
```

Step by step, with a shot list you write or edit yourself:

```bash
page-walkthrough capture recap.html --out work/
page-walkthrough plan work/ --seconds 90 --note "focus on the pricing table"   # or write work/shots.json by hand
page-walkthrough preview work/
page-walkthrough render work/ --out recap.mp4
```

A shot list looks like this:

```json
{
  "format": "portrait",
  "tts": "auto",
  "shots": [
    {"boxes": ["b0"], "say": "Here is the whole report in ninety seconds. Three findings, one decision."},
    {"rect": [95, 828, 470, 440], "move": "hold", "say": "This chart is the one to remember: costs fell while usage doubled."}
  ]
}
```

## Voices

<!-- filled in after the voice comparison -->

## Requirements

| For | Needs | Notes |
|---|---|---|
| Everything | Python 3.10+ and ffmpeg | Pillow, websockets and pypdfium2 install with the package |
| HTML pages | Chrome, Chromium or Edge | Found automatically; or set `CHROME=/path/to/browser` |
| PDFs | nothing extra | Rendered with pdfium; sections found from the page's white space, so scanned PDFs work |
| A voice | one of the options above | `page-walkthrough doctor` shows what it found |
| Auto-planning | `anthropic` and `ANTHROPIC_API_KEY` | Not needed when an agent writes the shot list |

## Licence

MIT
