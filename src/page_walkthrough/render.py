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
    words = text.split()
    chunks, cur = [], []
    for wd in words:
        cur.append(wd)
        if len(cur) >= max_words or (re.search(r"[.!?:;]$", wd) and len(cur) >= 3):
            chunks.append(" ".join(cur))
            cur = []
    if cur:
        chunks.append(" ".join(cur))
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


def render(folder, out=None, log=print):
    d, meta, boxes, job = load_job(folder)
    fmt, (W, H, band, view), plan = build_plan(d, meta, boxes, job, with_audio=True)
    page = Image.open(d / "page.png").convert("RGB")
    bgc = parse_bg(meta.get("bg"))
    scale = meta["scale"]
    total = sum(p["dur"] for p in plan) + TAIL_S
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
                    "-af", f"apad=pad_dur={TAIL_S}", str(audio)], check=True)

    out = Path(out) if out else d / "walkthrough.mp4"
    ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                           "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-i", str(audio),
                           "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
                           "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", str(out)],
                          stdin=subprocess.PIPE)
    canvas = Image.new("RGB", (W, H), bgc)
    for n in range(nframes):
        t = n / FPS
        k = max(i for i, p in enumerate(plan) if p["t"] <= t)
        p = plan[k]
        local = t - p["t"]
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
        canvas.paste(bgc, (0, 0, W, H))
        fr = frame_at(page, scale, win, view)
        if spot:
            fr = spotlight(fr, win, rect, view, 0.42)
        canvas.paste(fr, (0, 0))
        dr = ImageDraw.Draw(canvas)
        dr.rectangle((0, H - band, W, H), fill=(17, 17, 17))
        dr.rectangle((0, H - band, int(W * min(1, t / total)), H - band + 6), fill=(99, 102, 241))
        cap = next((c for c in p["caps"] if c[0] <= t < c[1]), None)
        if cap:
            lines = wrap(dr, cap[2], capf, W - 120)
            lh = int(fsize * 1.25)
            y0 = H - band + (band - lh * len(lines)) // 2
            for j, ln in enumerate(lines):
                tw = dr.textlength(ln, font=capf)
                dr.text(((W - tw) / 2, y0 + j * lh), ln, font=capf, fill=(255, 255, 255))
        ff.stdin.write(canvas.tobytes())
        if n % (FPS * 10) == 0:
            log(f"  {t:5.1f}s / {total:.1f}s")
    ff.stdin.close()
    if ff.wait() != 0:
        sys.exit("ffmpeg failed")
    log(f"{out}  ({total:.1f}s, {W}x{H}, {len(plan)} shots)")
    return out


