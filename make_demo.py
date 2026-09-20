#!/usr/bin/env python3
"""
make_demo.py — build the small demo course that ships with this repo.

Course Studio normally plays a VideoGenerator build (see README, "The one
external dependency"). That is a separate, heavier project, so this script
writes a tiny course by hand instead: three sections, a self-playing page, and
narration synthesised locally with Kokoro.

It exists for two reasons. It makes `python3 server.py` do something the moment
you clone the repo, and it is the shortest readable statement of the contract a
course has to satisfy:

    courses/<id>/
      course.json          sections with t0/t1, so the player can seek
      site/index.html      a page that plays itself against an <audio>
      site/voice.mp3       the narration

Anything meeting that contract plays. VideoGenerator is one producer of it; this
script is another, deliberately minimal one.

    python3 make_demo.py                 # needs a Kokoro server for audio
    python3 make_demo.py --no-audio      # silent, still plays
"""

from __future__ import annotations

import argparse
import json
import subprocess
import urllib.request
from pathlib import Path

KOKORO = "http://127.0.0.1:8880/v1/audio/speech"

# (kicker, title, narration, headline, bullets)
SECTIONS = [
    ("Part 1", "A course that is a web page",
     "This is not a video file. It is a web page that plays itself against an audio track. "
     "Every slide you see is live markup, which is what makes the rest of this possible.",
     "No video file",
     ["The page drives itself from one audio element",
      "Every slide is live markup, not pixels",
      "So it can change while you are watching"]),
    ("Part 2", "Ask while it runs",
     "You can ask a question at any point. If the course already answers it, you get the section "
     "and the timestamp, and you can jump straight there.",
     "Already covered?",
     ["The question is matched against the narration",
      "You get a section and a timestamp back",
      "One click seeks the player to that moment"]),
    ("Part 3", "Questions become slides",
     "When the course does not cover your question, it writes a new slide, narrates it, and drops "
     "it in at the end of the section you were watching. That is the part a finished video cannot do.",
     "Not covered yet",
     ["A new slide is written for your question",
      "It is narrated in the same voice",
      "It is inserted at the end of that section"]),
]


def synth(text: str, out: Path) -> float:
    """One Kokoro call per section. Returns the clip's duration in seconds."""
    req = urllib.request.Request(
        KOKORO, method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"model": "kokoro-v1", "voice": "af_heart",
                         "input": text}).encode())
    with urllib.request.urlopen(req, timeout=180) as r:
        out.write_bytes(r.read())          # Kokoro answers with WAV; it is re-encoded on concat
    return probe(out)


def probe(p: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(p)],
                       capture_output=True, text=True)
    return float(r.stdout.strip() or 0)


PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<title>{title}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Montserrat:wght@500;600;700;800&family=JetBrains+Mono:wght@500&display=swap">
<style>
:root{{--navy:#154899;--org:#F98F2C;--ink:#10151d;--body:#334155;--mut:#7d8ba0;--bg:#fff;--ln:#e6edf7}}
@media (prefers-color-scheme:dark){{
  :root{{--ink:#eef3fb;--body:#b7c4d6;--mut:#7f8da3;--bg:#0d1117;--ln:#222b3a}}
}}
*{{box-sizing:border-box;margin:0}}
html,body{{height:100%}}
body{{background:var(--bg);color:var(--ink);overflow:hidden;
  font-family:Montserrat,system-ui,-apple-system,sans-serif}}
#stage{{position:relative;width:100vw;height:100vh;container-type:size}}

.frame{{position:absolute;inset:0;display:flex;flex-direction:column;justify-content:center;
  padding:0 9cqw;opacity:0;transition:opacity .5s ease;pointer-events:none}}
.frame.on{{opacity:1}}

.kicker{{display:flex;align-items:center;gap:1.4cqw;
  font-family:'JetBrains Mono',monospace;font-weight:500;font-size:1.35cqw;
  letter-spacing:.26em;text-transform:uppercase;color:var(--org);margin-bottom:1.9cqw}}
.kicker s{{width:4.5cqw;height:.28cqw;background:var(--org);border-radius:99px;display:block;
  text-decoration:none}}

h2{{font-weight:800;font-size:5.6cqw;line-height:1.05;letter-spacing:-.028em;max-width:68cqw}}

h3{{font-weight:700;font-size:2.35cqw;line-height:1.25;color:var(--navy);
  margin:3.4cqw 0 1.8cqw;letter-spacing:-.012em}}
@media (prefers-color-scheme:dark){{ h3{{color:#6fa4ff}} }}

ul{{list-style:none;padding:0;display:flex;flex-direction:column;gap:1.35cqw;max-width:62cqw}}
li{{position:relative;padding-left:3.6cqw;font-size:1.95cqw;font-weight:500;line-height:1.45;
  color:var(--body);opacity:0;transform:translateY(12px);
  transition:opacity .55s ease,transform .55s ease}}
.on li{{opacity:1;transform:none}}
.on li:nth-child(2){{transition-delay:.16s}}
.on li:nth-child(3){{transition-delay:.32s}}
li::before{{content:"";position:absolute;left:0;top:.62em;width:1.9cqw;height:.26cqw;
  background:var(--org);border-radius:99px}}

/* section counter, bottom right — gives the empty corner a job */
.num{{position:absolute;right:9cqw;bottom:6.5cqw;font-family:'JetBrains Mono',monospace;
  font-weight:500;font-size:1.3cqw;letter-spacing:.1em;color:var(--mut)}}
.num b{{color:var(--ink);font-weight:500}}

#bar{{position:fixed;left:0;bottom:0;height:.42cqw;width:0;z-index:5;
  background:linear-gradient(90deg,var(--navy),var(--org))}}

/* play overlay: a real affordance, not a stray pill */
#cta{{position:fixed;inset:0;z-index:6;display:grid;place-items:center;cursor:pointer;
  border:0;padding:0;background:color-mix(in srgb,var(--bg) 72%,transparent);
  backdrop-filter:blur(3px);font-family:inherit}}
#cta[hidden]{{display:none}}
#cta span{{width:9cqw;height:9cqw;border-radius:50%;display:grid;place-items:center;
  background:var(--navy);box-shadow:0 1.4cqw 4cqw rgba(21,72,153,.34);
  transition:transform .18s ease}}
#cta:hover span{{transform:scale(1.06)}}
#cta span::after{{content:"";border-left:2.5cqw solid #fff;
  border-top:1.55cqw solid transparent;border-bottom:1.55cqw solid transparent;
  margin-left:.75cqw}}
</style>
<div id="stage">{frames}</div>
<div id="bar"></div>
<button id="cta" aria-label="Play"><span></span></button>
<audio id="voice" preload="auto" src="voice.mp3"></audio>
<script>
// The contract: each .frame carries t0/t1, and one <audio> decides which is visible.
// Course Studio's player seeks this audio; everything else follows from timeupdate.
const A = document.getElementById('voice'), F = [...document.querySelectorAll('.frame')],
      BAR = document.getElementById('bar'), CTA = document.getElementById('cta');
const DUR = {duration};
function paint(t){{
  F.forEach(f => f.classList.toggle('on', t >= +f.dataset.t0 && t < +f.dataset.t1));
  BAR.style.width = Math.min(100, t / DUR * 100) + '%';
}}
A.addEventListener('timeupdate', () => paint(A.currentTime));
A.addEventListener('play',  () => CTA.hidden = true);
A.addEventListener('pause', () => CTA.hidden = false);
CTA.addEventListener('click', () => A.play());
paint(0);
</script>
</html>"""



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="courses/demo")
    ap.add_argument("--no-audio", action="store_true")
    a = ap.parse_args()

    out = Path(a.out); site = out / "site"; site.mkdir(parents=True, exist_ok=True)
    parts, t, sections, frames = [], 0.0, [], []

    for i, (kicker, title, say, head, bullets) in enumerate(SECTIONS, 1):
        if a.no_audio:
            dur = 2.0 + len(say) / 16.0          # a readable pace, so the page still works silently
        else:
            clip = site / f"_p{i}.wav"
            dur = synth(say, clip) or 8.0
            parts.append(clip)
        t0, t1 = t, t + dur
        sections.append({"id": f"s{i}", "num": i, "title": title, "kicker": kicker,
                         "t0": round(t0, 2), "t1": round(t1, 2),
                         "at": f"{int(t0)//60}:{int(t0)%60:02d}",
                         "frame_from": i - 1, "frame_to": i - 1})
        lis = "".join(f"<li>{b}</li>" for b in bullets)
        frames.append(
            f'<section class="frame" data-t0="{t0:.2f}" data-t1="{t1:.2f}">'
            f'<div class="kicker"><s></s>{kicker}</div><h2>{title}</h2>'
            f'<h3>{head}</h3><ul>{lis}</ul>'
            f'<div class="num"><b>{i:02d}</b> / {len(SECTIONS):02d}</div></section>')
        t = t1

    if parts:
        lst = site / "_parts.txt"
        lst.write_text("".join(f"file '{p.name}'\n" for p in parts))
        # Speech, mono, 64k: small enough to keep the demo in the repo without regret.
        subprocess.run(["ffmpeg", "-v", "error", "-f", "concat", "-safe", "0",
                        "-i", str(lst), "-c:a", "libmp3lame", "-b:a", "64k", "-ac", "1",
                        str(site / "voice.mp3"), "-y"], check=True)
        for p in parts + [lst]:
            p.unlink()
        t = probe(site / "voice.mp3") or t
    else:
        (site / "voice.mp3").write_bytes(b"")

    (site / "index.html").write_text(
        PAGE.format(title="Course Studio — demo", frames="".join(frames), duration=round(t, 2)))
    (out / "course.json").write_text(json.dumps({
        "id": out.name,
        "title": "How Course Studio works",
        "brand": "",
        "duration": round(t, 2),
        "audio": "voice.mp3",
        "sections": sections,
        "beats": [{"n": i, "t0": s["t0"], "t1": s["t1"], "say": SECTIONS[i - 1][2],
                   "section": s["id"]} for i, s in enumerate(sections, 1)],
        "frames": [], "generated": [], "source_build": "make_demo.py",
    }, indent=1))

    print(f"demo -> {out}  ({len(sections)} sections, {t:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
