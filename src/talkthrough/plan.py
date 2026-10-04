"""Write shots.json automatically: Claude reads the captured page and decides what to show and say."""
import base64
import io
import json
from pathlib import Path

from PIL import Image

Image.MAX_IMAGE_PIXELS = None

MODEL = "claude-opus-5"
CHUNK_CSS = 1400
IMG_W = 800

SYSTEM = """You turn a document page into a narrated walkthrough video of that same page: a camera moves over \
the real page, one section at a time, while a voice explains it. You write the shot list.

What to show:
- Decide what a busy reader most needs from this page. Follow the page's own signals of importance: its title \
and opening promise, sections it calls key or essential, the central diagram or table, the decisions, the \
takeaways, the call to action at the end.
- Skip what is meant to be looked up rather than heard: long reference tables, appendices, source lists, \
boilerplate, navigation. A walkthrough is a guided tour, not a full read-out.
- Go in page order. One shot per idea. Frame the smallest set of boxes that shows that idea.
- For a wide diagram, use two or three shots with a rect that covers part of it, and narrate the part in frame.

What to say:
- Only what the page says. Paraphrase for the ear, but add no fact, number, name, example or claim that is not \
on the page. If the page hedges, you hedge.
- Open with the page's hook in one or two sentences. Close with the page's own takeaway or next step.
- Each shot: 1 to 3 short sentences, 12 to 45 words. Point at what is on screen ("this diagram", "the second \
row"). Spell out symbols a voice would stumble on. No markdown, no code syntax, no parentheses, no em dashes.

Fields: boxes lists box ids to frame (their union). rect is [x, y, width, height] in the page's css px and \
overrides boxes when non-empty; leave it empty unless you need part of a box. move is auto unless you have a \
reason: pan scrolls down a tall region, zoom pushes in slowly, hold stays still."""

SCHEMA = {
    "type": "object",
    "properties": {
        "shots": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "boxes": {"type": "array", "items": {"type": "string"}},
                    "rect": {"type": "array", "items": {"type": "number"}},
                    "move": {"type": "string", "enum": ["auto", "pan", "zoom", "hold"]},
                    "say": {"type": "string"},
                },
                "required": ["boxes", "rect", "move", "say"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["shots"],
    "additionalProperties": False,
}


def _page_images(folder, meta):
    page = Image.open(folder / "page.png").convert("RGB")
    s = meta["scale"]
    out = []
    for y in range(0, meta["height"], CHUNK_CSS):
        h = min(CHUNK_CSS, meta["height"] - y)
        crop = page.crop((0, y * s, meta["width"] * s, (y + h) * s))
        crop = crop.resize((IMG_W, max(1, round(crop.height * IMG_W / crop.width))), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        out.append((y, h, base64.standard_b64encode(buf.getvalue()).decode()))
    return out


def plan(folder, seconds=90, wpm=175, model=MODEL, extra=""):
    import anthropic

    folder = Path(folder)
    meta = json.loads((folder / "page.json").read_text())
    boxes = json.loads((folder / "boxes.json").read_text())
    text = (folder / "page.txt").read_text()
    box_lines = "\n".join(f'{b["id"]} {b["tag"]} {b.get("cls", "")[:24]} {b["rect"]} {b["text"][:110]}'
                          for b in boxes[:500])
    words = int(seconds * wpm / 60)
    content = []
    for y, h, data in _page_images(folder, meta):
        content.append({"type": "text", "text": f"Page image, css y {y} to {y + h}:"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}})
    content.append({"type": "text", "text":
                    f"Page: {meta.get('title', '')}, {meta['width']} x {meta['height']} css px.\n\n"
                    f"Boxes (id, tag, class, [x, y, w, h], text):\n{box_lines}\n\n"
                    f"Full page text:\n{text[:60000]}\n\n"
                    f"Target: about {seconds} seconds, so about {words} words of narration in total. {extra}"})

    client = anthropic.Anthropic()
    resp = client.beta.messages.create(
        model=model,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": content}],
        thinking={"type": "adaptive"},
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        extra_body={"fallbacks": "default"},
    )
    if resp.stop_reason == "refusal":
        raise SystemExit("The model declined to plan this page.")
    if resp.stop_reason == "max_tokens":
        raise SystemExit("The plan was cut off; try a shorter target length.")
    raw = next(b.text for b in resp.content if b.type == "text")
    shots = json.loads(raw)["shots"]
    ids = {b["id"] for b in boxes}
    clean = []
    for s in shots:
        shot = {"say": s["say"].strip(), "move": s["move"]}
        if len(s["rect"]) == 4:
            shot["rect"] = [round(v) for v in s["rect"]]
        else:
            good = [i for i in s["boxes"] if i in ids]
            if not good:
                continue
            shot["boxes"] = good
        clean.append(shot)
    return clean
