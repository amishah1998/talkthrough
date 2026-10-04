"""Write a three-shot shots.json from whatever capture found, so the smoke test does not depend on box ids."""
import json
import sys
from pathlib import Path

work = Path(sys.argv[1])
boxes = [b for b in json.loads((work / "boxes.json").read_text()) if b["tag"] != "page"]
if len(boxes) < 3:
    sys.exit(f"capture found only {len(boxes)} sections in {work}")
lines = ["This is the opening of the page.", "Here is the next section.", "And this is where it ends."]
picks = [boxes[0], boxes[len(boxes) // 2], boxes[-1]]
shots = [{"boxes": [b["id"]], "say": say} for b, say in zip(picks, lines)]
(work / "shots.json").write_text(json.dumps({"format": "landscape", "tts": "kokoro", "shots": shots}, indent=1))
print(f"{len(shots)} shots from {len(boxes)} sections -> {work / 'shots.json'}")
