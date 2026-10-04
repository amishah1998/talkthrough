"""Capture a page into a folder: page.png (the whole page), boxes.json (its sections), page.txt, page.json."""
import asyncio
import base64
import io
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from PIL import Image

Image.MAX_IMAGE_PIXELS = None


def find_chrome():
    if os.environ.get("CHROME"):
        return os.environ["CHROME"]
    candidates = {
        "Darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                   "/Applications/Chromium.app/Contents/MacOS/Chromium",
                   "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
        "Windows": [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"],
    }.get(platform.system(), [])
    for c in candidates:
        if os.path.exists(c):
            return c
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome", "msedge"):
        p = shutil.which(name)
        if p:
            return p
    sys.exit("No Chrome, Chromium or Edge found. Install one, or set CHROME=/path/to/browser.")


BOXES_JS = r"""
(() => {
  const sel = 'h1,h2,h3,header,section,article,figure,svg,table,pre,img,canvas,blockquote,ol,ul,p,details,' +
              '[class*=slide],[class*=card],[class*=tile],[class*=callout],[class*=seg],[class*=diagram],[class*=box],' +
              'aside,[class*=good],[class*=warn],[class*=note],[class*=tip],[class*=info],[class*=alert],[class*=panel]';
  const seen = new Set(), out = [];
  for (const el of document.querySelectorAll(sel)) {
    if (el.closest('svg') && el.tagName.toLowerCase() !== 'svg') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 80 || r.height < 24) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || +st.opacity === 0) continue;
    const x = Math.round(r.left + scrollX), y = Math.round(r.top + scrollY);
    const key = [x, y, Math.round(r.width), Math.round(r.height)].join(',');
    if (seen.has(key)) continue;
    seen.add(key);
    let depth = 0; for (let p = el.parentElement; p; p = p.parentElement) depth++;
    const text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 140);
    out.push({tag: el.tagName.toLowerCase(), cls: (el.getAttribute('class') || '').slice(0, 60),
              rect: [x, y, Math.round(r.width), Math.round(r.height)], depth, text});
    if (out.length >= 600) break;
  }
  return out;
})()
"""

FREEZE_JS = r"""
(() => {
  const target = location.hash ? document.getElementById(decodeURIComponent(location.hash.slice(1))) : null;
  if (target) {
    if (target.tagName === 'DETAILS') target.open = true;
    for (let d = target.closest('details'); d; d = d.parentElement && d.parentElement.closest('details')) d.open = true;
  }
  const s = document.createElement('style');
  s.textContent = '*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important}';
  document.head.appendChild(s);
  for (const el of document.querySelectorAll('*')) {
    const p = getComputedStyle(el).position;
    if (p === 'fixed') el.style.setProperty('display', 'none', 'important');
    else if (p === 'sticky') el.style.position = 'static';
  }
  return true;
})()
"""


# ---------- capture (Chrome over the DevTools protocol) ----------

class CDP:
    def __init__(self, ws):
        self.ws, self.n, self.events = ws, 0, []

    async def call(self, method, **params):
        self.n += 1
        mid = self.n
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            self.events.append(msg)

    async def wait_event(self, name, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            if any(e.get("method") == name for e in self.events):
                return
            try:
                msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=1))
                self.events.append(msg)
            except asyncio.TimeoutError:
                pass
        raise TimeoutError(name)

    async def js(self, expr):
        r = await self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _capture(url, width, scale):
    import websockets
    port = _free_port()
    profile = tempfile.mkdtemp(prefix="walkthrough-chrome-")
    proc = subprocess.Popen([find_chrome(), "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
                             "--hide-scrollbars", "--no-first-run", "--no-default-browser-check", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws_url = None
        for _ in range(100):
            try:
                targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1))
                ws_url = next(t["webSocketDebuggerUrl"] for t in targets if t.get("type") == "page")
                break
            except Exception:
                time.sleep(0.1)
        if not ws_url:
            sys.exit("The browser did not start. Set CHROME=/path/to/browser.")
        async with websockets.connect(ws_url, max_size=None) as ws:
            c = CDP(ws)
            await c.call("Page.enable")
            await c.call("Emulation.setDeviceMetricsOverride", width=width, height=1200,
                         deviceScaleFactor=scale, mobile=False)
            await c.call("Page.navigate", url=url)
            await c.wait_event("Page.loadEventFired")
            for _ in range(30):
                title = await c.js("document.title + ' ' + (document.body ? document.body.innerText.slice(0, 200) : '')")
                if not re.search(r"just a moment|checking your browser|attention required|verify you are human",
                                 title or "", re.I):
                    break
                await asyncio.sleep(1)
            else:
                print("warning: the page still shows a bot check; capture a saved copy instead", file=sys.stderr)
            await c.js("document.fonts ? document.fonts.ready.then(() => true) : true")
            await asyncio.sleep(0.8)
            await c.js(FREEZE_JS)
            await asyncio.sleep(0.3)
            height = int(await c.js("Math.ceil(document.documentElement.scrollHeight)"))
            boxes = [b for b in await c.js(BOXES_JS) if b["rect"][1] + b["rect"][3] <= height + 2]
            text = await c.js("document.body.innerText")
            title = await c.js("document.title")
            bg = await c.js("getComputedStyle(document.body).backgroundColor")
            await c.call("Emulation.setDeviceMetricsOverride", width=width, height=min(height, 8000),
                         deviceScaleFactor=scale, mobile=False)
            await asyncio.sleep(0.3)
            page = Image.new("RGB", (width * scale, height * scale), "white")
            tile = 3000
            for y in range(0, height, tile):
                h = min(tile, height - y)
                shot = await c.call("Page.captureScreenshot", format="png", captureBeyondViewport=True,
                                    clip={"x": 0, "y": y, "width": width, "height": h, "scale": 1})
                im = Image.open(io.BytesIO(base64.b64decode(shot["data"]))).convert("RGB")
                page.paste(im, (0, y * scale))
            try:
                await c.call("Browser.close")
            except Exception:
                pass
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)

    meta = {"url": url, "title": title, "width": width, "height": height, "scale": scale, "bg": bg, "kind": "html"}
    return page, text, meta, boxes


PDF_GAP = 24


def _profile(mask, box, axis):
    """Which rows (axis=0) or columns (axis=1) of box contain ink."""
    x0, y0, x1, y1 = box
    region = mask.crop(box)
    size = (1, y1 - y0) if axis == 0 else (x1 - x0, 1)
    return [v > 0 for v in region.resize(size, Image.Resampling.BOX).getdata()]


def _runs(ink, min_gap):
    """Spans of ink separated by at least min_gap empty cells."""
    spans, start, gap = [], None, 0
    for i, on in enumerate(ink):
        if on:
            if start is None:
                start = i
            gap = 0
            end = i + 1
        elif start is not None:
            gap += 1
            if gap >= min_gap:
                spans.append((start, end))
                start = None
    if start is not None:
        spans.append((start, end))
    return spans


def _xy_cut(mask, box, depth, out, gap_y=10, gap_x=14):
    """Recursive whitespace cut: bands of content, then columns inside a band. Records every node."""
    x0, y0, x1, y1 = box
    rows = _runs(_profile(mask, box, 0), 1)
    cols = _runs(_profile(mask, box, 1), 1)
    if not rows or not cols:
        return
    box = x0 + cols[0][0], y0 + rows[0][0], x0 + cols[-1][1], y0 + rows[-1][1]
    x0, y0, x1, y1 = box
    if x1 - x0 >= 40 and y1 - y0 >= 12:
        out.append((box, depth))
    if depth >= 4:
        return
    bands = _runs(_profile(mask, box, 0), gap_y)
    if len(bands) > 1:
        for a, b in bands:
            _xy_cut(mask, (x0, y0 + a, x1, y0 + b), depth + 1, out, gap_y, gap_x)
        return
    columns = _runs(_profile(mask, box, 1), gap_x)
    if len(columns) > 1:
        for a, b in columns:
            _xy_cut(mask, (x0 + a, y0, x0 + b, y1), depth + 1, out, gap_y, gap_x)


def _undouble(word):
    """Outlined SVG text often extracts with every glyph twice ("TTHHEE"); collapse such words."""
    if len(word) >= 4 and len(word) % 2 == 0 and all(word[i] == word[i + 1] for i in range(0, len(word), 2)):
        return word[::2]
    return word


def _ink_mask(img, bg):
    """White where the page differs from its background colour."""
    from PIL import ImageChops
    diff = ImageChops.difference(img, Image.new("RGB", img.size, bg)).convert("L")
    return diff.point(lambda v: 255 if v > 18 else 0)


def capture_pdf(path, out, scale):
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(str(path))
    sizes = [pdf[i].get_size() for i in range(len(pdf))]
    width = int(max(s[0] for s in sizes)) + 2 * PDF_GAP
    height = int(sum(s[1] for s in sizes) + PDF_GAP * (len(sizes) + 1))
    sheet = Image.new("RGB", (width * scale, height * scale), (232, 232, 235))
    boxes, texts, y = [], [], PDF_GAP
    for i in range(len(pdf)):
        page = pdf[i]
        pw, ph = sizes[i]
        x = (width - pw) / 2
        img = page.render(scale=scale).to_pil().convert("RGB")
        sheet.paste(img, (int(x * scale), int(y * scale)))
        tp = page.get_textpage()
        text = tp.get_text_range()
        texts.append(f"--- page {i + 1} ---\n{text}")
        boxes.append({"tag": "page", "cls": f"page {i + 1}", "rect": [round(x), round(y), round(pw), round(ph)],
                      "depth": 0, "text": " ".join(text.split())[:140]})
        small = img.resize((round(pw), round(ph)), Image.Resampling.BOX)
        bg = max(small.getcolors(small.width * small.height), key=lambda c: c[0])[1]
        nodes = []
        _xy_cut(_ink_mask(small, bg), (0, 0, small.width, small.height), 1, nodes)
        seen = set()
        for (bx0, by0, bx1, by1), depth in nodes:
            if (bx0, by0, bx1, by1) in seen or (bx1 - bx0) * (by1 - by0) > 0.9 * pw * ph:
                continue
            seen.add((bx0, by0, bx1, by1))
            bt = " ".join(_undouble(w) for w in tp.get_text_bounded(bx0, ph - by1, bx1, ph - by0).split())
            boxes.append({"tag": "region", "cls": f"page {i + 1}", "depth": depth, "text": bt[:140],
                          "rect": [round(x + bx0), round(y + by0), bx1 - bx0, by1 - by0]})
        y += ph + PDF_GAP
    title = (pdf.get_metadata_dict().get("Title") or "").strip() or re.sub(r"[-_]+", " ", Path(path).stem)
    meta = {"url": Path(path).resolve().as_uri(), "title": title, "width": width, "height": height,
            "scale": scale, "bg": "rgb(232, 232, 235)", "kind": "pdf", "pages": len(pdf)}
    return sheet, "\n\n".join(texts), meta, boxes


def capture(src, out, width=820, scale=2):
    out = Path(out)
    if str(src).lower().endswith(".pdf") and not re.match(r"^https?:", str(src)):
        page, text, meta, boxes = capture_pdf(src, out, scale)
    else:
        url = src if re.match(r"^(https?|file):", str(src)) else Path(src).resolve().as_uri()
        page, text, meta, boxes = asyncio.run(_capture(url, width, scale))
    for i, b in enumerate(boxes):
        b["id"] = f"b{i}"
    out.mkdir(parents=True, exist_ok=True)
    page.save(out / "page.png")
    (out / "page.txt").write_text(text or "")
    (out / "page.json").write_text(json.dumps(meta, indent=1))
    (out / "boxes.json").write_text(json.dumps(boxes, indent=1, ensure_ascii=False))
    return meta, boxes
