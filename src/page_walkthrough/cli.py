"""page-walkthrough: turn an HTML page or a PDF into a narrated walkthrough video of itself."""
import argparse
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from pathlib import Path

from . import __version__


def _job_path(folder):
    return Path(folder) / "shots.json"


def cmd_capture(a):
    from .capture import capture
    meta, boxes = capture(a.page, a.out, a.width, a.scale)
    print(f"captured {meta['title']!r}: {meta['width']}x{meta['height']} css px at {meta['scale']}x, "
          f"{len(boxes)} boxes -> {a.out}")


def _need_planner():
    try:
        import anthropic  # noqa: F401
    except ImportError:
        sys.exit("planning needs the anthropic package: pip install "
                 "'page-walkthrough[plan] @ git+https://github.com/amishah1998/page-walkthrough'")
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        sys.exit("planning needs ANTHROPIC_API_KEY. Without one, ask Claude Code for the video (the plugin plans "
                 "it for you), or run capture, write shots.json yourself, then preview and render.")


def cmd_plan(a):
    _need_planner()
    from .plan import plan
    shots = plan(a.dir, seconds=a.seconds, model=a.model, extra=a.note or "")
    job = {"format": a.format, "tts": a.tts, "shots": shots}
    _job_path(a.dir).write_text(json.dumps(job, indent=1, ensure_ascii=False))
    print(f"{len(shots)} shots -> {_job_path(a.dir)}")


def cmd_preview(a):
    from .render import preview
    preview(a.dir)


def _mac_default_browser():
    """Bundle id of the app that handles https links, or None if the user never changed it (Safari)."""
    prefs = Path.home() / "Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist"
    try:
        handlers = plistlib.loads(prefs.read_bytes()).get("LSHandlers", [])
    except (OSError, plistlib.InvalidFileException):
        return None
    for scheme in ("https", "http"):
        for h in handlers:
            if h.get("LSHandlerURLScheme") == scheme and h.get("LSHandlerRoleAll"):
                return h["LSHandlerRoleAll"]
    return None


def open_in_browser(video):
    """Play the finished video in the default web browser.

    On macOS a plain open hands an .mp4 to QuickTime, so the browser is looked up and named explicitly."""
    video = Path(video).resolve()
    if platform.system() == "Darwin":
        bundle = _mac_default_browser()
        args = ["open", "-b", bundle, str(video)] if bundle else ["open", "-a", "Safari", str(video)]
        if subprocess.run(args, capture_output=True).returncode == 0:
            return
    if not webbrowser.open(video.as_uri()):
        print(f"could not open a browser; the video is at {video}", file=sys.stderr)


def cmd_render(a):
    from .render import render
    out = render(a.dir, a.out, quality="share" if a.share else None, speed=a.speed)
    if a.open:
        open_in_browser(out)


def cmd_make(a):
    _need_planner()
    from .capture import capture
    from .plan import plan
    from .render import preview, render
    folder = Path(a.keep) if a.keep else Path(tempfile.mkdtemp(prefix="page-walkthrough-"))
    capture(a.page, folder, a.width, a.scale)
    shots = plan(folder, seconds=a.seconds, model=a.model, extra=a.note or "")
    _job_path(folder).write_text(json.dumps({"format": a.format, "tts": a.tts, "shots": shots}, indent=1,
                                            ensure_ascii=False))
    preview(folder)
    out = Path(a.out) if a.out else Path.cwd() / (Path(a.page).stem + "-walkthrough.mp4")
    render(folder, out, quality="share" if a.share else None, speed=a.speed)
    if not a.keep:
        shutil.rmtree(folder, ignore_errors=True)
    if a.open:
        open_in_browser(out)


def cmd_doctor(a):
    from .capture import find_chrome
    ok = True

    def row(name, good, note):
        nonlocal ok
        ok &= good or name.startswith("(optional)")
        print(f"  {'ok ' if good else '-- '} {name}: {note}")

    print(f"page-walkthrough {__version__} on {platform.system()}")
    try:
        row("browser", True, find_chrome())
    except SystemExit as e:
        row("browser", False, str(e))
    row("ffmpeg", bool(shutil.which("ffmpeg")), shutil.which("ffmpeg") or "install ffmpeg")
    row("ffprobe", bool(shutil.which("ffprobe")), shutil.which("ffprobe") or "comes with ffmpeg")
    from .voice import ORDER, kokoro_available
    voices = [name for name, env in ORDER if os.environ.get(env)]
    if kokoro_available():
        voices.append("kokoro (local)")
    if platform.system() == "Darwin" and shutil.which("say"):
        voices.append("say (macOS)")
    row("voice", bool(voices), ", ".join(voices) or "none: set CARTESIA_API_KEY, ELEVENLABS_API_KEY or OPENAI_API_KEY, "
        "or pip install 'page-walkthrough[local] @ git+https://github.com/amishah1998/page-walkthrough'")
    try:
        import anthropic  # noqa: F401
        has_sdk = True
    except ImportError:
        has_sdk = False
    auto = has_sdk and bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    row("(optional) auto-plan", auto, "ready" if auto else
        "needs pip install 'page-walkthrough[plan] @ git+https://github.com/amishah1998/page-walkthrough' and ANTHROPIC_API_KEY; not needed when an agent writes shots.json")
    sys.exit(0 if ok else 1)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="page-walkthrough", description=__doc__)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def capture_args(p):
        p.add_argument("--width", type=int, default=820, help="viewport width in css px for HTML (default 820)")
        p.add_argument("--scale", type=int, default=2, help="capture pixel density (default 2)")

    def plan_args(p):
        p.add_argument("--seconds", type=int, default=90, help="target length (default 90)")
        p.add_argument("--format", default="portrait", choices=["portrait", "landscape", "square"])
        p.add_argument("--tts", default="auto", choices=["auto", "cartesia", "elevenlabs", "openai", "kokoro", "say"])
        p.add_argument("--model", default="claude-opus-5", help="Claude model that plans the shots")
        p.add_argument("--note", help="extra direction for the planner, e.g. 'focus on the pricing table'")

    p = sub.add_parser("capture", help="screenshot the page and list its sections")
    p.add_argument("page", help=".html, .pdf, or an http(s) URL")
    p.add_argument("--out", required=True, help="work folder")
    capture_args(p)
    p.set_defaults(fn=cmd_capture)

    p = sub.add_parser("plan", help="let Claude write shots.json for a captured page")
    p.add_argument("dir")
    plan_args(p)
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("preview", help="one spotlighted frame per shot, as preview.png")
    p.add_argument("dir")
    p.set_defaults(fn=cmd_preview)

    p = sub.add_parser("render", help="voice, camera and captions into an MP4")
    p.add_argument("dir")
    p.add_argument("--out")
    p.add_argument("--share", action="store_true", help="smaller file for chat apps (about half the size)")
    p.add_argument("--speed", type=float, help="voice pace, e.g. 1.25 or 1.5 (default 1.0)")
    p.add_argument("--open", action="store_true", help="play the video in your web browser when it is done")
    p.set_defaults(fn=cmd_render)

    p = sub.add_parser("make", help="capture, plan, preview and render in one go")
    p.add_argument("page")
    p.add_argument("--out", help="output .mp4 (default: <page>-walkthrough.mp4)")
    p.add_argument("--keep", help="keep the work folder here")
    p.add_argument("--share", action="store_true", help="smaller file for chat apps (about half the size)")
    p.add_argument("--speed", type=float, help="voice pace, e.g. 1.25 or 1.5 (default 1.0)")
    p.add_argument("--open", action="store_true", help="play the video in your web browser when it is done")
    capture_args(p)
    plan_args(p)
    p.set_defaults(fn=cmd_make)

    p = sub.add_parser("doctor", help="check what is installed")
    p.set_defaults(fn=cmd_doctor)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
