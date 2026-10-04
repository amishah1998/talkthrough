"""Turn a captured page plus shots.json into a narrated MP4: camera moves, spotlight, captions."""
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .voice import GAP_S, synthesize

Image.MAX_IMAGE_PIXELS = None

FORMATS = {"portrait": (1080, 1920), "landscape": (1920, 1080), "square": (1080, 1080)}
FPS = 30
MOVE_S = 0.8
TAIL_S = 1.2
PAD = 24
FONT_PATHS = ["/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/Helvetica.ttc",
              "C:\\Windows\\Fonts\\segoeui.ttf", "C:\\Windows\\Fonts\\arial.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans.ttf"]
# Fonts that carry Devanagari and Latin, so Hindi in a caption or title is not drawn as empty boxes.
DEVANAGARI_FONT_PATHS = ["/System/Library/Fonts/Kohinoor.ttc", "/System/Library/Fonts/Supplemental/DevanagariMT.ttc",
                         "C:\\Windows\\Fonts\\Nirmala.ttc", "C:\\Windows\\Fonts\\NirmalaUI.ttf",
                         "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
                         "/usr/share/fonts/noto/NotoSansDevanagari-Regular.ttf"]
_font_paths = FONT_PATHS


def has_devanagari(text):
    return any("\u0900" <= ch <= "\u097f" for ch in text)


# ---------- shot list -> camera timeline ----------

def load_job(d):
    d = Path(d)
    meta = json.loads((d / "page.json").read_text())
    boxes = {b["id"]: b for b in json.loads((d / "boxes.json").read_text())}
    try:
        job = json.loads((d / "shots.json").read_text())
    except FileNotFoundError:
        sys.exit(f"no shots.json in {d}; write one, or run talkthrough plan {d}")
    except json.JSONDecodeError as e:
        sys.exit(f"{d / 'shots.json'} is not valid JSON: {e}")
    if not job.get("shots"):
        sys.exit(f"{d / 'shots.json'} has no shots")
    for i, shot in enumerate(job["shots"]):
        if not shot.get("say", "").strip():
            sys.exit(f"shot {i} in {d / 'shots.json'} has nothing to say")
        if not (shot.get("rect") or shot.get("boxes") or shot.get("box")):
            sys.exit(f"shot {i} in {d / 'shots.json'} needs boxes or a rect")
    return d, meta, boxes, job


def ink_mask(page):
    """Pixels that differ from the page background, so dark pages snap as well as white ones."""
    grey = page.convert("L")
    hist = grey.histogram()
    bg = hist.index(max(hist))
    return grey.point(lambda v: 255 if abs(v - bg) > 24 else 0)


def _bands(proj, min_gap):
    """Runs of ink in a projection, split wherever at least min_gap empty cells separate them."""
    out, start, gap, last = [], None, 0, 0
    for i, v in enumerate(proj):
        if v:
            if start is None:
                start = i
            gap, last = 0, i
        elif start is not None:
            gap += 1
            if gap >= min_gap:
                out.append((start, last + 1))
                start = None
    if start is not None:
        out.append((start, last + 1))
    return out


def _trim_cut_edges(ink, rect, tight, s):
    """Drop a thin edge band that the rect slices through: it touches the rect's edge and runs on past it.

    Typical case: a box drawn around one panel of a figure also catches the left half of the shared caption
    and a sliver of the next panel's border. Only bands at the rect's own edges qualify, so the trim never
    cascades through the lines of a paragraph."""
    X0, Y0, X1, Y1 = rect
    x0, y0, x1, y1 = tight
    near, skip, touch, gap = round(6 * s), round(2 * s), round(1 * s), round(2 * s)

    def inked(box):
        x, y, xx, yy = max(0, box[0]), max(0, box[1]), min(ink.width, box[2]), min(ink.height, box[3])
        return xx > x and yy > y and ink.crop((x, y, xx, yy)).getbbox() is not None

    def runs_past_sides(box):
        bb = ink.crop(box).getbbox()
        if not bb:
            return False
        bx0, by0, bx1, by1 = box[0] + bb[0], box[1] + bb[1], box[0] + bb[2], box[1] + bb[3]
        # Look a little way past the edge, not right at it: a glyph's overhang (the tail of a j, a serif)
        # stops within a pixel or two, while content the rect actually cut keeps going.
        return (bx0 <= X0 + touch and inked((X0 - near, by0, X0 - skip, by1))) or \
            (bx1 >= X1 - touch and inked((X1 + skip, by0, X1 + near, by1))) or \
            (by0 <= Y0 + touch and inked((bx0, Y0 - near, bx1, Y0 - skip))) or \
            (by1 >= Y1 - touch and inked((bx0, Y1 + skip, bx1, Y1 + near)))

    rows = [(y0 + a, y0 + b) for a, b in _bands(ink.crop((x0, y0, x1, y1)).getprojection()[1], gap)]
    if len(rows) > 1:
        a, b = rows[-1]
        if b - a < 0.2 * (y1 - y0) and runs_past_sides((x0, a, x1, b)):
            y1 = rows[-2][1]
        a, b = rows[0]
        if b - a < 0.2 * (y1 - y0) and runs_past_sides((x0, a, x1, b)):
            y0 = rows[1][0]
    cols = [(x0 + a, x0 + b) for a, b in _bands(ink.crop((x0, y0, x1, y1)).getprojection()[0], gap)]
    if len(cols) > 1:
        a, b = cols[-1]
        if b - a < 0.2 * (x1 - x0) and runs_past_sides((a, y0, b, y1)):
            x1 = cols[-2][1]
        a, b = cols[0]
        if b - a < 0.2 * (x1 - x0) and runs_past_sides((a, y0, b, y1)):
            x0 = cols[1][0]
    bb = ink.crop((x0, y0, x1, y1)).getbbox()
    return (x0 + bb[0], y0 + bb[1], x0 + bb[2], y0 + bb[3]) if bb else tight


def snap(rect, pad, ink, scale):
    """Shrink rect to the ink inside it, then pad each side only as far as the white space allows.

    Rects are often placed by eye, and a fixed pad pushed the outline into the next column or caption."""
    x, y, w, h = rect
    s = scale
    x0, y0, x1, y1 = round(x * s), round(y * s), round((x + w) * s), round((y + h) * s)
    tight = ink.crop((x0, y0, x1, y1)).getbbox()
    if not tight:
        return rect
    x0, y0, x1, y1 = _trim_cut_edges(ink, (x0, y0, x1, y1), (x0 + tight[0], y0 + tight[1], x0 + tight[2], y0 + tight[3]), s)
    # A glyph that overhangs its element box by a pixel belongs to the content, not to a neighbour;
    # left outside, it made the pad collapse to one pixel and the outline sat on the first letter.
    touch = max(1, round(s))
    for _ in range(3):
        grown = (x0, y0, x1, y1)
        if x0 > 0 and ink.crop((x0 - touch, y0, x0, y1)).getbbox():
            x0 -= touch
        if x1 < ink.width and ink.crop((x1, y0, x1 + touch, y1)).getbbox():
            x1 += touch
        if y0 > 0 and ink.crop((x0, y0 - touch, x1, y0)).getbbox():
            y0 -= touch
        if y1 < ink.height and ink.crop((x0, y1, x1, y1 + touch)).getbbox():
            y1 += touch
        if (x0, y0, x1, y1) == grown:
            break
    reach = round(pad * s)
    look = 2 * reach
    top = ink.crop((x0, max(0, y0 - look), x1, y0)).getbbox()
    bottom = ink.crop((x0, y1, x1, min(ink.height, y1 + look))).getbbox()
    left = ink.crop((max(0, x0 - look), y0, x0, y1)).getbbox()
    right = ink.crop((x1, y0, min(ink.width, x1 + look), y1)).getbbox()
    gaps = [
        min(look, y0) - top[3] if top else look,
        bottom[1] if bottom else look,
        min(look, x0) - left[2] if left else look,
        right[0] if right else look,
    ]
    # Meet a neighbour halfway rather than touching it.
    pt, pb, pl, pr = (max(1, min(reach, g // 2)) for g in gaps)
    return (x0 - pl) / s, (y0 - pt) / s, (x1 - x0 + pl + pr) / s, (y1 - y0 + pt + pb) / s


def shot_rect(shot, boxes, meta, ink=None):
    if "rect" in shot:
        x, y, w, h = shot["rect"]
    else:
        ids = shot.get("boxes") or [shot["box"]]
        missing = [i for i in ids if i not in boxes]
        if missing:
            sys.exit(f"unknown box id(s) {missing}; see boxes.json")
        rs = [boxes[i]["rect"] for i in ids]
        x0, y0 = min(r[0] for r in rs), min(r[1] for r in rs)
        x1, y1 = max(r[0] + r[2] for r in rs), max(r[1] + r[3] for r in rs)
        x, y, w, h = x0, y0, x1 - x0, y1 - y0
    pad = shot.get("pad", PAD)
    if ink is not None and shot.get("snap", True):
        x, y, w, h = snap((x, y, w, h), pad, ink, meta["scale"])
    else:
        x, y = x - pad, y - pad
        w, h = w + 2 * pad, h + 2 * pad
    x, y = max(0, x), max(0, y)
    w, h = min(meta["width"] - x, w), min(meta["height"] - y, h)
    return x, y, w, h


def layout(fmt):
    W, H = FORMATS[fmt]
    band = {"portrait": 380, "landscape": 190, "square": 230}[fmt]
    return W, H, band, (W, H - band)


def fit_window(rect, view, meta, min_w):
    """Smallest window of the view's aspect that shows rect (by width), centred on it."""
    x, y, w, h = rect
    va = view[0] / view[1]
    ww = max(w, min_w, h * va)
    ww = min(ww, meta["width"])
    wh = ww / va
    cx, cy = x + w / 2, y + h / 2
    return clamp_win((cx - ww / 2, cy - wh / 2, ww, wh), meta)


def clamp_win(win, meta):
    x, y, w, h = win
    x = min(max(0, x), max(0, meta["width"] - w))
    y = min(max(0, y), max(0, meta["height"] - h))
    return (x, y, w, h)


def shot_moves(rect, view, meta, move, min_w):
    """Start and end window for one shot: pan down a tall box, or a slow push-in."""
    va = view[0] / view[1]
    x, y, w, h = rect
    full = fit_window(rect, view, meta, min_w)
    tall = h > (max(w, min_w) / va) * 1.08
    if move == "auto":
        move = "pan" if tall else "zoom"
    if move == "pan":
        ww = min(max(w, min_w), meta["width"])
        wh = ww / va
        cx = x + w / 2
        start = clamp_win((cx - ww / 2, y, ww, wh), meta)
        end = clamp_win((cx - ww / 2, y + h - wh, ww, wh), meta)
        return start, end
    if move == "zoom":
        k = 0.94
        fx, fy, fw, fh = full
        end = clamp_win((fx + fw * (1 - k) / 2, fy + fh * (1 - k) / 2, fw * k, fh * k), meta)
        if end[2] < min_w * 0.98:
            end = full
        return full, end
    return full, full


def ease(t):
    return t * t * (3 - 2 * t)


def lerp_win(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(4))


# ---------- captions ----------

def font(size):
    for p in _font_paths:
        if os.path.exists(p):
            try:
                f = ImageFont.truetype(p, size)
                try:
                    f.set_variation_by_name("Semibold")
                except Exception:
                    pass
                return f
            except Exception:
                continue
    return ImageFont.load_default()


def caption_chunks(text, max_words):
    """Sentences first, then long sentences split into even pieces, so no caption straddles two sentences."""
    chunks = []
    for sent in re.split(r"(?<=[.!?])\s+", text.strip()):
        words = sent.split()
        if not words:
            continue
        n = math.ceil(len(words) / max_words)
        size = math.ceil(len(words) / n)
        chunks += [" ".join(words[i:i + size]) for i in range(0, len(words), size)]
    return chunks


def timed_captions(p, max_words):
    """Caption chunks with start and end times: from the voice's own word timings when it gave them,
    otherwise spread over the shot's speech by word count."""
    chunks = caption_chunks(p["say"], max_words)
    caps = []
    words = p.get("words")
    if words and len(words) >= sum(len(c.split()) for c in chunks) * 0.8:
        k = 0
        for c in chunks:
            n = len(c.split())
            seg = words[k:k + n] or words[-1:]
            caps.append((p["t"] + seg[0][1], p["t"] + seg[-1][2] + 0.15, c))
            k += n
        for m in range(len(caps) - 1):
            caps[m] = (caps[m][0], max(caps[m][1], caps[m + 1][0]), caps[m][2])
        return caps
    nw = sum(len(c.split()) for c in chunks) or 1
    speak = p["dur"] - GAP_S
    acc = 0
    for c in chunks:
        c0 = p["t"] + speak * acc / nw
        acc += len(c.split())
        caps.append((c0, p["t"] + speak * acc / nw, c))
    return caps


def wrap(draw, text, f, max_w):
    lines, cur = [], ""
    for wd in text.split():
        t = (cur + " " + wd).strip()
        if draw.textlength(t, font=f) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    if len(lines) == 2:
        words = text.split()
        best = min(range(1, len(words)),
                   key=lambda k: max(draw.textlength(" ".join(words[:k]), font=f),
                                     draw.textlength(" ".join(words[k:]), font=f)))
        a, b = " ".join(words[:best]), " ".join(words[best:])
        if max(draw.textlength(a, font=f), draw.textlength(b, font=f)) <= max_w:
            lines = [a, b]
    return lines


# ---------- rendering ----------

def parse_bg(s):
    m = re.findall(r"[\d.]+", s or "")
    if len(m) >= 3 and (len(m) < 4 or float(m[3]) > 0):
        return tuple(int(float(v)) for v in m[:3])
    return (255, 255, 255)


def build_plan(d, meta, boxes, job, with_audio):
    fmt = job.get("format", "portrait")
    W, H, band, view = layout(fmt)
    scale = meta["scale"]
    min_w = view[0] / scale / 1.15
    plan, t = [], 0.0
    audio_dir = d / "audio"
    if with_audio:
        audio_dir.mkdir(exist_ok=True)
    ink = ink_mask(Image.open(d / "page.png"))
    for i, s in enumerate(job["shots"]):
        rect = shot_rect(s, boxes, meta, ink)
        start, end = shot_moves(rect, view, meta, s.get("move", "auto"), min_w)
        dur, words = None, None
        if with_audio:
            dur, words = synthesize(s["say"], job, audio_dir / f"shot{i:02d}.wav")
        spot = s.get("spotlight", job.get("spotlight", True))
        plan.append({"i": i, "t": t, "dur": dur, "start": start, "end": end, "say": s["say"],
                     "rect": rect, "spot": bool(spot), "words": words})
        if dur:
            t += dur
    return fmt, (W, H, band, view), plan


def frame_at(page, scale, win, view):
    x, y, w, h = win
    box = (x * scale, y * scale, (x + w) * scale, (y + h) * scale)
    return page.resize(view, Image.Resampling.BICUBIC, box=box, reducing_gap=2.0)


def overlaps(a, b):
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def spotlight(frame, win, rect, view, alpha, strength=1.0):
    """Dim everything outside rect and outline it, so the eye lands on the section being narrated."""
    wx, wy, ww, wh = win
    sx, sy = view[0] / ww, view[1] / wh
    x0, y0 = (rect[0] - wx) * sx, (rect[1] - wy) * sy
    x1, y1 = (rect[0] + rect[2] - wx) * sx, (rect[1] + rect[3] - wy) * sy
    if x0 <= 2 and y0 <= 2 and x1 >= view[0] - 2 and y1 >= view[1] - 2:
        return frame
    mask = Image.new("L", view, int(255 * alpha))
    ImageDraw.Draw(mask).rounded_rectangle((x0, y0, x1, y1), radius=18, fill=0)
    lit = Image.composite(Image.new("RGB", view, (24, 24, 27)), frame, mask)
    ImageDraw.Draw(lit).rounded_rectangle((x0, y0, x1, y1), radius=18, outline=(99, 102, 241), width=4)
    return lit if strength >= 1 else Image.blend(frame, lit, strength)


def preview(folder):
    d, meta, boxes, job = load_job(folder)
    fmt, (W, H, band, view), plan = build_plan(d, meta, boxes, job, with_audio=False)
    page = Image.open(d / "page.png").convert("RGB")
    thumbs = []
    for p in plan:
        im = frame_at(page, meta["scale"], p["start"], view)
        if p["spot"]:
            im = spotlight(im, p["start"], p["rect"], view, 0.42)
        im.thumbnail((360, 360 * view[1] // view[0]))
        thumbs.append(im)
    cols = 4 if fmt == "portrait" else 3
    tw, th = thumbs[0].size
    rows = math.ceil(len(thumbs) / cols)
    sheet = Image.new("RGB", (cols * (tw + 16) + 16, rows * (th + 44) + 16), (30, 30, 30))
    dr = ImageDraw.Draw(sheet)
    f = font(20)
    for k, im in enumerate(thumbs):
        cx, cy = 16 + (k % cols) * (tw + 16), 16 + (k // cols) * (th + 44)
        sheet.paste(im, (cx, cy + 28))
        dr.text((cx, cy), f"shot {k}", fill=(255, 255, 255), font=f)
    out = d / "preview.png"
    sheet.save(out)
    words = sum(len(s["say"].split()) for s in job["shots"])
    wpm = job.get("rate") or 175
    print(f"{out}\n{len(plan)} shots, {words} words, about {words / wpm * 60 + len(plan) * GAP_S:.0f}s of narration")
    return out


INTRO_S = 2.2
OUTRO_S = 3.2
FADE_S = 0.45
CREDIT = "made with talkthrough  ·  github.com/amishah1998/talkthrough"
ENCODE = {
    "hq": ["-preset", "medium", "-crf", "22"],
    "share": ["-preset", "slow", "-crf", "27", "-tune", "stillimage"],
}


def page_title(job, meta):
    if job.get("title"):
        return job["title"]
    t = (meta.get("title") or "").strip()
    for sep in (" - ", " | "):
        head, _, tail = t.rpartition(sep)
        if head and len(head) >= 20 and len(tail) <= 40:
            t = head
    return t or "Walkthrough"


def page_link(job, meta):
    if job.get("link"):
        return job["link"]
    url = meta.get("url") or ""
    if url.startswith(("http://", "https://")):
        return re.sub(r"^https?://(www\.)?", "", url.split("#")[0]).rstrip("/")
    return Path(url.split("#")[0]).name


def _backdrop(frame, size):
    from PIL import ImageFilter
    bg = frame.resize(size, Image.Resampling.BICUBIC).filter(ImageFilter.GaussianBlur(22))
    return Image.blend(bg, Image.new("RGB", size, (12, 12, 16)), 0.72)


def _centered(dr, lines, fnt, y, W, fill, gap=1.22):
    size = fnt.size
    for ln in lines:
        tw = dr.textlength(ln, font=fnt)
        dr.text(((W - tw) / 2, y), ln, font=fnt, fill=fill)
        y += int(size * gap)
    return y


def title_card(first, W, H, title, kicker):
    card = _backdrop(first, (W, H))
    dr = ImageDraw.Draw(card)
    big = font(int(W * (0.075 if H > W else 0.05)))
    small = font(int(W * (0.03 if H > W else 0.02)))
    lines = wrap(dr, title, big, W - 160)[:5]
    block = len(lines) * int(big.size * 1.22) + small.size * 3
    y = (H - block) // 2
    dr.rectangle(((W - 90) // 2, y - 50, (W + 90) // 2, y - 42), fill=(99, 102, 241))
    y = _centered(dr, lines, big, y, W, (255, 255, 255))
    _centered(dr, [kicker], small, y + small.size, W, (199, 210, 254))
    return card


def end_card(last, W, H, link, credit):
    card = _backdrop(last, (W, H))
    dr = ImageDraw.Draw(card)
    mid = font(int(W * (0.055 if H > W else 0.038)))
    small = font(int(W * (0.026 if H > W else 0.017)))
    y = H // 2 - mid.size * 2
    y = _centered(dr, ["Read the full page"], mid, y, W, (255, 255, 255))
    if link:
        y = _centered(dr, wrap(dr, link, small, W - 140), small, y + small.size, W, (199, 210, 254))
    if credit:
        _centered(dr, [CREDIT], font(int(small.size * 0.8)), H - int(H * 0.06), W, (140, 140, 150))
    return card


def render(folder, out=None, log=print, quality=None, speed=None):
    global _font_paths
    d, meta, boxes, job = load_job(folder)
    spoken = " ".join(sh["say"] for sh in job["shots"]) + " " + page_title(job, meta)
    _font_paths = FONT_PATHS
    if has_devanagari(spoken):
        _font_paths = DEVANAGARI_FONT_PATHS + FONT_PATHS
        from PIL import features
        log("note: the narration has Devanagari. Captions use a Hindi font, but the voices are tuned for English "
            "and Hinglish in Roman letters, so they may read it badly."
            + ("" if features.check("raqm") else " For correctly joined Hindi letters, install libraqm."))
    if speed:
        job["speed"] = speed
    fmt, (W, H, band, view), plan = build_plan(d, meta, boxes, job, with_audio=True)
    page = Image.open(d / "page.png").convert("RGB")
    bgc = parse_bg(meta.get("bg"))
    scale = meta["scale"]
    quality = quality or job.get("quality", "hq")
    intro = INTRO_S if job.get("intro", True) else 0.0
    outro = OUTRO_S if job.get("outro", True) else TAIL_S
    for p in plan:
        p["t"] += intro
    speech_end = plan[-1]["t"] + plan[-1]["dur"]
    total = speech_end + outro
    nframes = int(total * FPS)
    fsize = {"portrait": 54, "landscape": 44, "square": 46}[fmt]
    capf = font(fsize)
    max_words = {"portrait": 7, "landscape": 10, "square": 8}[fmt]
    for p in plan:
        p["caps"] = timed_captions(p, max_words)

    listfile = d / "audio" / "list.txt"
    listfile.write_text("".join(f"file 'shot{p['i']:02d}.wav'\n" for p in plan))
    audio = d / "audio" / "all.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listfile),
                    "-af", f"adelay={int(intro * 1000)}:all=1,apad=pad_dur={outro},loudnorm=I=-16:TP=-1.5:LRA=11",
                    "-ar", "48000", str(audio)], check=True)

    def view_frame(t):
        k = max(i for i, p in enumerate(plan) if p["t"] <= t) if t >= plan[0]["t"] else 0
        p = plan[k]
        local = max(0.0, t - p["t"])
        rect, spot, strength = p["rect"], p["spot"], 1.0
        if k > 0 and local < MOVE_S:
            prev = plan[k - 1]
            e = ease(local / MOVE_S)
            win = lerp_win(prev["end"], p["start"], e)
            if overlaps(prev["end"], p["start"]):
                rect = lerp_win(prev["rect"], p["rect"], e)
                spot = p["spot"] or prev["spot"]
            else:
                # On a long jump a sliding box would land on whatever the camera passes; fade it instead.
                rect, spot = (prev["rect"], prev["spot"]) if e < 0.5 else (p["rect"], p["spot"])
                strength = abs(1 - 2 * e)
        else:
            span = max(0.01, p["dur"] - (MOVE_S if k > 0 else 0))
            u = min(1.0, (local - (MOVE_S if k > 0 else 0)) / span)
            win = lerp_win(p["start"], p["end"], ease(u))
        fr = frame_at(page, scale, win, view)
        if spot and strength > 0.02:
            fr = spotlight(fr, win, rect, view, 0.42, strength)
        return fr, p

    def shot_frame(t):
        fr, p = view_frame(t)
        canvas = Image.new("RGB", (W, H), bgc)
        canvas.paste(fr, (0, 0))
        dr = ImageDraw.Draw(canvas)
        dr.rectangle((0, H - band, W, H), fill=(17, 17, 17))
        cap = next((c for c in p["caps"] if c[0] <= t < c[1]), None)
        if cap:
            lines = wrap(dr, cap[2], capf, W - 120)
            lh = int(fsize * 1.25)
            y0 = H - band + (band - lh * len(lines)) // 2
            for j, ln in enumerate(lines):
                tw = dr.textlength(ln, font=capf)
                dr.text(((W - tw) / 2, y0 + j * lh), ln, font=capf, fill=(255, 255, 255))
        return canvas

    mins, secs = divmod(round(total), 60)
    first_card = last_card = None
    if intro:
        first_card = title_card(view_frame(plan[0]["t"])[0], W, H, page_title(job, meta),
                                f"WALKTHROUGH  ·  {mins}:{secs:02d}")
    if job.get("outro", True):
        last_card = end_card(view_frame(speech_end - 0.05)[0], W, H, page_link(job, meta), job.get("credit", True))

    out = Path(out) if out else d / "walkthrough.mp4"
    ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                           "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-i", str(audio),
                           "-c:v", "libx264", *ENCODE[quality], "-pix_fmt", "yuv420p",
                           "-c:a", "aac", "-b:a", "128k" if quality == "hq" else "96k",
                           "-shortest", "-movflags", "+faststart", str(out)],
                          stdin=subprocess.PIPE)
    for n in range(nframes):
        t = n / FPS
        if first_card is not None and t < intro:
            frame = first_card
            if t > intro - FADE_S:
                frame = Image.blend(first_card, shot_frame(intro), (t - (intro - FADE_S)) / FADE_S)
        elif last_card is not None and t >= speech_end:
            a = min(1.0, (t - speech_end) / FADE_S)
            frame = last_card if a >= 1 else Image.blend(shot_frame(speech_end - 0.05), last_card, a)
        else:
            frame = shot_frame(t)
            ImageDraw.Draw(frame).rectangle((0, H - band, int(W * min(1, t / total)), H - band + 6),
                                            fill=(99, 102, 241))
        ff.stdin.write(frame.tobytes())
        if n % (FPS * 10) == 0:
            log(f"  {t:5.1f}s / {total:.1f}s")
    ff.stdin.close()
    if ff.wait() != 0:
        sys.exit("ffmpeg failed")
    size = out.stat().st_size / 1e6
    log(f"{out}  ({total:.1f}s, {W}x{H}, {len(plan)} shots, {size:.1f} MB)")
    return out
