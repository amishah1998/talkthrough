# Contributing

Issues and pull requests are welcome.

## Set up

```bash
git clone https://github.com/amishah1998/talkthrough
cd talkthrough
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[all]"
talkthrough doctor
```

## Before you open a PR

- Render one real video with your change and look at a few frames from it
  (`ffmpeg -ss 10 -i out.mp4 -frames:v 1 frame.png`), including one mid-transition.
  Framing bugs only show up in real frames.
- If you touch narration rules, keep the one promise: the voice says only what the page says.
- Keep the code dependency-light. Pillow, websockets and pypdfium2 are the only hard dependencies.

## Reporting a bad video

Attach the page (or a link to it), the `shots.json`, and a frame that shows the problem.
