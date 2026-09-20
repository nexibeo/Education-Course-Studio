#!/usr/bin/env python3
"""
Ingest a VideoGenerator build into a playable, question-answerable course.

The insight this rests on: VideoGenerator already renders its video FROM a
self-playing web page. `site/index.html` holds one `<section class="frame">`
per frame with `t0`/`t1` attributes, and an `<audio>` element drives which
frame is visible via `timeupdate`. The mp4 is a recording of that page.

So a course player does not need to render or even touch video. It serves the
same page, adds section navigation around it, and layers a question panel on
top. Every visual — the simulated product screens, the presenter inset, the
triangle motif, the mono captions — is inherited exactly, because it is
literally the same HTML.

What this script produces (`course.json`):

    sections[]   one per `chapter` frame, with its time range and the frames
                 that belong to it
    beats[]      the narration timeline: start, end, and the spoken text,
                 which is what makes "that is covered at 4:12" possible
    generated[]  slides appended later by the Q&A layer, kept separate from
                 the original build so a re-ingest never loses them

Usage:
    python3 ingest.py --build /path/to/VideoGenerator/in-assistant/hotel \
                      --out courses/hotel
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path


def mmss(seconds: float) -> str:
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}:{s:02d}"


def parse_planned(planned) -> tuple[float, float] | None:
    """beats.json carries `planned: ["0:22", "0:34"]` as its reliable timing."""
    if not isinstance(planned, list) or len(planned) != 2:
        return None
    out = []
    for v in planned:
        if isinstance(v, (int, float)):
            out.append(float(v))
            continue
        m = re.match(r"^(\d+):(\d{1,2})(?:\.(\d+))?$", str(v).strip())
        if not m:
            return None
        out.append(int(m.group(1)) * 60 + int(m.group(2)) + (float("0." + m.group(3)) if m.group(3) else 0))
    return out[0], out[1]


def load_beats(build: Path) -> list[dict]:
    p = build / "work" / "beats.json"
    if not p.exists():
        return []
    raw = json.loads(p.read_text())
    beats = raw.get("beats") if isinstance(raw, dict) else raw
    out = []
    for b in beats or []:
        span = parse_planned(b.get("planned"))
        start = b.get("start") if isinstance(b.get("start"), (int, float)) else (span[0] if span else None)
        end = b.get("end") if isinstance(b.get("end"), (int, float)) else (span[1] if span else None)
        vo = (b.get("vo") or "").strip()
        if start is None or not vo:
            continue
        out.append(
            {
                "n": b.get("n"),
                "type": b.get("type"),
                "start": float(start),
                "end": float(end) if end is not None else float(start),
                "vo": vo,
            }
        )
    return out


def build_sections(frames: list[dict], duration: float) -> list[dict]:
    """
    A `chapter` frame opens a section; everything until the next chapter
    belongs to it. A build with no chapter frames becomes one section, so the
    player still works rather than showing nothing.
    """
    chapters = [i for i, f in enumerate(frames) if f.get("type") == "chapter"]
    if not chapters:
        return [
            {
                "id": "s1",
                "num": 1,
                "title": "Full course",
                "t0": 0.0,
                "t1": duration,
                "frame_from": 0,
                "frame_to": len(frames),
            }
        ]

    sections = []
    for idx, fi in enumerate(chapters):
        f = frames[fi]
        nxt = chapters[idx + 1] if idx + 1 < len(chapters) else len(frames)
        t0 = float(f.get("t0") or 0.0)
        t1 = float(frames[nxt].get("t0")) if nxt < len(frames) else float(duration)
        sections.append(
            {
                "id": f"s{idx + 1}",
                "num": f.get("num") or (idx + 1),
                "title": (f.get("title") or f"Section {idx + 1}").strip(),
                "kicker": (f.get("kicker") or "").strip(),
                "t0": t0,
                "t1": t1,
                "at": mmss(t0),
                "frame_from": fi,
                "frame_to": nxt,
            }
        )
    return sections


def attach_beats(sections: list[dict], beats: list[dict]) -> None:
    """Bind each beat to the section it plays in, so a citation can name it."""
    for b in beats:
        b["section_id"] = None
        b["at"] = mmss(b["start"])
        for s in sections:
            if s["t0"] <= b["start"] < s["t1"]:
                b["section_id"] = s["id"]
                break
        if b["section_id"] is None and sections:
            b["section_id"] = sections[-1]["id"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", required=True, help="VideoGenerator audience build dir")
    ap.add_argument("--out", required=True, help="course output dir")
    ap.add_argument("--title", default=None)
    args = ap.parse_args()

    build = Path(args.build).expanduser()
    site = build / "site"
    spec_path = site / "sections.json"
    if not spec_path.exists():
        spec_path = build / "sections.json"
    if not spec_path.exists():
        print(f"!! no sections.json under {build}")
        return 1

    spec = json.loads(spec_path.read_text())
    frames = spec.get("frames") or []
    duration = float(spec.get("duration") or 0)

    sections = build_sections(frames, duration)
    beats = load_beats(build)
    attach_beats(sections, beats)

    out = Path(args.out).expanduser()
    (out / "site").mkdir(parents=True, exist_ok=True)

    # Copy the built site verbatim — this is the whole visual system, and
    # rewriting any of it would be how the look starts drifting.
    for name in ("index.html", "voice.mp3", "sections.json", "edl-mixed.json"):
        src = site / name
        if src.exists():
            shutil.copy2(src, out / "site" / name)

    course = {
        "id": out.name,
        "title": args.title or spec.get("title") or out.name,
        "brand": spec.get("brand"),
        "duration": duration,
        "audio": spec.get("audio") or "voice.mp3",
        "sections": sections,
        "beats": beats,
        "frames": frames,
        "generated": [],  # Q&A-authored slides live here, never in `frames`
        "source_build": str(build),
    }
    (out / "course.json").write_text(json.dumps(course, indent=2))

    print(f"  {course['title']}")
    print(f"  {duration/60:.1f} min · {len(frames)} frames · {len(sections)} sections · {len(beats)} beats")
    for s in sections:
        n = sum(1 for b in beats if b["section_id"] == s["id"])
        print(f"    {s['at']:>5}  {s['num']}. {s['title'][:52]}  ({n} beats)")
    print(f"  -> {out/'course.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
