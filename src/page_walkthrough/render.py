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


# ---------- shot list -> camera timeline ----------

def load_job(d):
    d = Path(d)
    meta = json.loads((d / "page.json").read_text())
    boxes = {b["id"]: b for b in json.loads((d / "boxes.json").read_text())}
    job = json.loads((d / "shots.json").read_text())
    return d, meta, boxes, job


def shot_rect(shot, boxes, meta):
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
    x, y = max(0, x - pad), max(0, y - pad)
    w, h = min(meta["width"] - x, w + 2 * pad), min(meta["height"] - y, h + 2 * pad)
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
    for p in FONT_PATHS:
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
    for i, s in enumerate(job["shots"]):
        rect = shot_rect(s, boxes, meta)
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


def spotlight(frame, win, rect, view, alpha):
    """Dim everything outside rect and outline it, so the eye lands on the section being narrated."""
    wx, wy, ww, wh = win
    sx, sy = view[0] / ww, view[1] / wh
    x0, y0 = (rect[0] - wx) * sx, (rect[1] - wy) * sy
    x1, y1 = (rect[0] + rect[2] - wx) * sx, (rect[1] + rect[3] - wy) * sy
    if x0 <= 2 and y0 <= 2 and x1 >= view[0] - 2 and y1 >= view[1] - 2:
        return frame
    mask = Image.new("L", view, int(255 * alpha))
    ImageDraw.Draw(mask).rounded_rectangle((x0, y0, x1, y1), radius=18, fill=0)
    frame = Image.composite(Image.new("RGB", view, (24, 24, 27)), frame, mask)
    ImageDraw.Draw(frame).rounded_rectangle((x0, y0, x1, y1), radius=18, outline=(99, 102, 241), width=4)
    return frame


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
CREDIT = "made with page-walkthrough  ·  github.com/amishah1998/page-walkthrough"
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
    d, meta, boxes, job = load_job(folder)
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
        rect, spot = p["rect"], p["spot"]
        if k > 0 and local < MOVE_S:
            e = ease(local / MOVE_S)
            win = lerp_win(plan[k - 1]["end"], p["start"], e)
            rect = lerp_win(plan[k - 1]["rect"], p["rect"], e)
            spot = p["spot"] or plan[k - 1]["spot"]
        else:
            span = max(0.01, p["dur"] - (MOVE_S if k > 0 else 0))
            u = min(1.0, (local - (MOVE_S if k > 0 else 0)) / span)
            win = lerp_win(p["start"], p["end"], ease(u))
        fr = frame_at(page, scale, win, view)
        if spot:
            fr = spotlight(fr, win, rect, view, 0.42)
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
