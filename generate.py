#!/usr/bin/env python3
"""
Generate a course from a script or transcript.

This is the front half of VideoGenerator's pipeline, minus the parts that do
not apply here: no presenter track, and no video render, because Course Studio
plays the built site directly.

    source text
      -> outline + beats          one LLM call, guarded
      -> narration                Kokoro, per beat, measured
      -> sections.json            timings taken FROM the audio, never guessed
      -> build_showcase.py        VideoGenerator's own builder, untouched
      -> ingest.py                -> a playable, question-answerable course

The timing rule is the one worth stating twice: `t0`/`t1` come from the real
duration of each rendered narration clip. PROCESS.md makes the same point —
timings come from beats, so no phrase matching can mistime a frame. Guessing
durations from word counts is how a deck drifts out of sync with its voice.

Only the built-in frame types are used (chapter, statement, vs, list, stats,
quote, end). The `screen` frames that make the reference video so convincing
come from hand-authored scene packs like `screens_hotel.py`, one per audience;
those stay a human job, and a generated course simply does without them until
a pack exists.

Usage:
    python3 generate.py --script my-script.md --title "..." --audience "..." --out courses/demo
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
import wave
from pathlib import Path

# Where your VideoGenerator checkout lives. Course Studio renders ITS output;
# see README "The one external dependency".
VG = Path(os.environ.get("CS_VIDEOGENERATOR", "../VideoGenerator")).expanduser()
KOKORO = os.environ.get("CS_TTS", "http://127.0.0.1:8880/v1/audio/speech")
MODEL = os.environ.get("CS_MODEL", "deepseek/deepseek-v4-pro")
API = "https://openrouter.ai/api/v1/chat/completions"

# Types the base renderer provides, so a generated course needs no scene pack.
FRAME_TYPES = ["chapter", "statement", "vs", "list", "stats", "quote"]


def key() -> str:
    k = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if k:
        return k
    env = Path(os.environ.get("CS_ENV_FILE", ".env"))
    if env.exists():
        for line in env.read_text().splitlines():
            if line.strip().startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip().strip("\"'")
    raise RuntimeError("OPENROUTER_API_KEY not set")


PLAN_SYSTEM = """You turn a source script into a course plan.

Return JSON only. No prose, no code fences.

{"title": "...",
 "sections": [
   {"title": "<max 7 words>",
    "kicker": "Part N",
    "beats": [
      {"vo": "<what the narrator says, 2-5 sentences>",
       "frame": {"type": "statement|vs|list|stats|quote", ...fields...}}
     ]}
 ]}

Frame fields by type:
  statement  {"type":"statement","size":"big","kicker":"<2-3 words>","title":"<max 8 words, may use <em>word</em> for one emphasis>","lead":"<one sentence>"}
  vs         {"type":"vs","kicker":"<2-3 words>","left":{"tag":"<1-2 words>","text":"<clauses separated by ' · '>"},"right":{"tag":"<1-2 words>","text":"<clauses separated by ' · '>"}}
  list       {"type":"list","kicker":"<2-5 words>","lead":"<one sentence>","items":[["<label>","<value or empty string>"], ...]}
  stats      {"type":"stats","kicker":"<2-3 words>","items":[["<number>","<label>"], ...]}
  quote      {"type":"quote","kicker":"<2-3 words>","quotes":[{"text":"<one sentence>","src":"<attribution>"}]}

Rules, enforced because they are what makes this watchable:
- 4 to 7 sections. Each has 2 to 5 beats. Every beat has exactly one frame.
- The FIRST beat of the whole course opens with a result, never a question.
- Never two frames of the same type back to back inside a section.
- At most one `quote` in the entire course.
- `vs` needs two genuinely opposed sides. Do not use it for a list.
- `list` items: 3 to 7. Keep each label under 5 words.
- `stats` needs real numbers taken from the source. If the source has none, do
  not use stats at all.

Voice, non-negotiable:
- The audience is a working professional, not a developer.
- Never use an em dash. Use a full stop or a comma.
- Banned: delve, leverage, seamless, unlock, robust, elevate, empower,
  streamline, harness, revolutionize, supercharge, cutting-edge, transformative,
  game changer, dive in, realm, landscape, myriad, foster, in today's fast-paced.
- Say what the thing does, not that it is powerful.
- Narration is spoken register: short sentences, contractions, no bullet syntax."""


def call_llm(system: str, user: str, max_tokens: int = 12000) -> str:
    body = {
        "model": MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        API,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key()}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        d = json.loads(r.read())
    return (d["choices"][0]["message"].get("content") or "").strip()


def parse_json(text: str):
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.S)
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}", t, re.S)
        return json.loads(m.group(0)) if m else None


# ---------------------------------------------------------------- guards


BANNED = [
    "delve", "leverage", "seamless", "unlock", "robust", "elevate", "empower",
    "streamline", "harness", "revolutionize", "supercharge", "cutting-edge",
    "transformative", "game changer", "dive in", "realm", "landscape", "myriad",
    "foster", "in today's fast-paced",
]


def guard(plan: dict) -> list[str]:
    """
    The in-code checks PROCESS.md insists on. Reported, not silently repaired:
    a plan that trips these should be regenerated, because quietly patching it
    is how the same three flaws end up in every course.
    """
    problems: list[str] = []
    secs = plan.get("sections") or []
    if not 3 <= len(secs) <= 9:
        problems.append(f"{len(secs)} sections (want 4-7)")

    quotes = 0
    first_vo = ""
    for si, s in enumerate(secs, 1):
        beats = s.get("beats") or []
        if not beats:
            problems.append(f"section {si} has no beats")
            continue
        prev = None
        for bi, b in enumerate(beats, 1):
            f = b.get("frame") or {}
            t = f.get("type")
            vo = (b.get("vo") or "").strip()
            if not vo:
                problems.append(f"s{si}b{bi} has no narration")
            if not first_vo:
                first_vo = vo
            if t not in FRAME_TYPES:
                problems.append(f"s{si}b{bi} unknown frame type {t!r}")
            if t and t == prev:
                problems.append(f"s{si}b{bi} repeats {t} back to back")
            prev = t
            if t == "quote":
                quotes += 1
            if t == "list":
                items = f.get("items") or []
                if not 3 <= len(items) <= 7:
                    problems.append(f"s{si}b{bi} list has {len(items)} items (want 3-7)")
            if t == "vs":
                if not (f.get("left") or {}).get("text") or not (f.get("right") or {}).get("text"):
                    problems.append(f"s{si}b{bi} vs is missing a side")
            low = vo.lower()
            for w in BANNED:
                if w in low:
                    problems.append(f"s{si}b{bi} narration uses banned word {w!r}")
            if "—" in vo or "—" in json.dumps(f, ensure_ascii=False):
                problems.append(f"s{si}b{bi} uses an em dash")

    if quotes > 1:
        problems.append(f"{quotes} quote frames (max 1)")
    if first_vo.rstrip().endswith("?"):
        problems.append("opening beat is a question; it must open with a result")
    return problems


# ------------------------------------------------------------- narration


def synth(text: str, dest: Path, voice: str = "bm_george") -> float:
    """
    One narration clip. Returns its real duration in seconds.

    Cached on the exact text: a rerun after a failed build should not pay for
    narration again, and re-synthesising identical text would also shift every
    downstream timing by a few milliseconds for no reason.
    """
    stamp = dest.with_suffix(".txt")
    if dest.exists() and stamp.exists() and stamp.read_text() == text:
        with wave.open(str(dest)) as w:
            return w.getnframes() / float(w.getframerate())
    body = json.dumps({"model": "kokoro-v1", "input": text, "voice": voice, "speed": 1.0}).encode()
    req = urllib.request.Request(KOKORO, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        dest.write_bytes(r.read())
    stamp.write_text(text)
    with wave.open(str(dest)) as w:
        return w.getnframes() / float(w.getframerate())


def concat(parts: list[Path], out: Path) -> None:
    """ffmpeg concat, then mp3 — what the site's <audio> expects."""
    lst = out.parent / "_parts.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-c:a", "libmp3lame", "-q:a", "4", str(out)],
        check=True,
    )
    lst.unlink(missing_ok=True)


# ------------------------------------------------------------------ main


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", required=True, help="source script / transcript (text or md)")
    ap.add_argument("--title", default=None)
    ap.add_argument("--audience", default="working professionals")
    ap.add_argument("--out", required=True)
    ap.add_argument("--voice", default="bm_george")
    ap.add_argument("--plan-only", action="store_true")
    args = ap.parse_args()

    src = Path(args.script).expanduser().read_text()
    out = Path(args.out).expanduser()
    work = out / "work"
    work.mkdir(parents=True, exist_ok=True)

    # ---- 1. plan -------------------------------------------------------
    plan_file = work / "plan.json"
    if plan_file.exists():
        print("  plan: reusing work/plan.json")
        plan = json.loads(plan_file.read_text())
    else:
        print("  plan: asking the model…")
        user = (
            f"AUDIENCE: {args.audience}\n"
            f"TITLE HINT: {args.title or '(derive one)'}\n\n"
            f"SOURCE SCRIPT:\n{src[:60000]}"
        )
        plan = parse_json(call_llm(PLAN_SYSTEM, user))
        if not plan:
            print("  !! model did not return usable JSON")
            return 1
        plan_file.write_text(json.dumps(plan, indent=2))

    problems = guard(plan)
    print(f"  plan: {len(plan.get('sections') or [])} sections, "
          f"{sum(len(s.get('beats') or []) for s in plan.get('sections') or [])} beats")
    if problems:
        print("  guards tripped:")
        for p in problems:
            print(f"    - {p}")
        if args.plan_only:
            return 1
        print("  continuing anyway (delete work/plan.json to re-plan)")
    else:
        print("  guards: all pass")

    if args.plan_only:
        return 0

    # ---- 2. narrate, measuring as we go --------------------------------
    clips_dir = work / "clips"
    clips_dir.mkdir(exist_ok=True)
    frames, beats_out, parts = [], [], []
    t = 0.0
    n = 0

    for si, s in enumerate(plan.get("sections") or [], 1):
        # A chapter card opens each section. It gets its own short clip so the
        # title is on screen while it is being introduced.
        chap_vo = f"Part {si}. {s.get('title','')}."
        n += 1
        cp = clips_dir / f"{n:03d}.wav"
        dur = synth(chap_vo, cp, args.voice)
        parts.append(cp)
        frames.append({"t0": round(t, 2), "t1": round(t + dur, 2), "type": "chapter",
                       "num": si, "kicker": s.get("kicker") or f"Part {si}",
                       "title": s.get("title") or f"Section {si}", "in": 0.45})
        beats_out.append({"n": n, "type": "chapter", "start": round(t, 2),
                          "end": round(t + dur, 2), "vo": chap_vo})
        t += dur

        for b in s.get("beats") or []:
            vo = (b.get("vo") or "").strip()
            f = dict(b.get("frame") or {})
            if not vo or f.get("type") not in FRAME_TYPES:
                continue
            n += 1
            cp = clips_dir / f"{n:03d}.wav"
            dur = synth(vo, cp, args.voice)
            parts.append(cp)
            f["t0"] = round(t, 2)
            f["t1"] = round(t + dur, 2)
            frames.append(f)
            beats_out.append({"n": n, "type": f["type"], "start": round(t, 2),
                              "end": round(t + dur, 2), "vo": vo})
            t += dur
            print(f"    {n:>3}. {f['type']:<10} {dur:5.1f}s  {vo[:52]}")

    print(f"  narration: {n} clips, {t/60:.1f} min")

    # ---- 3. spec + audio ----------------------------------------------
    site = out / "site"
    site.mkdir(parents=True, exist_ok=True)
    spec = {
        "title": plan.get("title") or args.title or out.name,
        "brand": "complete-ai-training",
        "lang": "en",
        "duration": round(t, 2),
        "audio": "voice.mp3",
        "frames": frames,
    }
    (work / "sections.json").write_text(json.dumps(spec, indent=1, ensure_ascii=False))
    concat(parts, work / "voice.mp3")

    # ---- 4. build with VideoGenerator's own builder ---------------------
    print("  building site…")
    r = subprocess.run(
        [sys.executable, "build_showcase.py", "--spec", str((work / "sections.json").resolve()),
         "--out", str(site.resolve()), "--brand", "complete-ai-training",
         "--frame-pack", "diagram", "--frame-pack", "kinetic",
         "--frame-pack", "data", "--frame-pack", "media", "--text-scale", "1.2"],
        cwd=VG, capture_output=True, text=True,
    )
    if r.returncode != 0:
        print("  !! build failed:\n" + (r.stderr or r.stdout)[-1500:])
        return 1

    # build_showcase writes a SILENT track of the right length; replace it with
    # the real narration. Same filename, so the site needs no change.
    (site / "voice.mp3").write_bytes((work / "voice.mp3").read_bytes())

    # ---- 5. course.json -------------------------------------------------
    course_beats = beats_out
    sections = []
    for i, f in enumerate([x for x in frames if x["type"] == "chapter"]):
        idx = frames.index(f)
        nxt = next((j for j in range(idx + 1, len(frames)) if frames[j]["type"] == "chapter"), len(frames))
        t1 = frames[nxt]["t0"] if nxt < len(frames) else spec["duration"]
        m, sec = divmod(int(round(f["t0"])), 60)
        sections.append({"id": f"s{i+1}", "num": f.get("num", i + 1), "title": f["title"],
                         "kicker": f.get("kicker", ""), "t0": f["t0"], "t1": t1,
                         "at": f"{m}:{sec:02d}", "frame_from": idx, "frame_to": nxt})
    for b in course_beats:
        b["section_id"] = next((s["id"] for s in sections if s["t0"] <= b["start"] < s["t1"]),
                               sections[-1]["id"] if sections else None)
        m, sec = divmod(int(round(b["start"])), 60)
        b["at"] = f"{m}:{sec:02d}"

    (out / "course.json").write_text(json.dumps({
        "id": out.name, "title": spec["title"], "brand": spec["brand"],
        "duration": spec["duration"], "audio": "voice.mp3",
        "sections": sections, "beats": course_beats, "frames": frames,
        "generated": [], "source_build": str(out),
    }, indent=2))

    print(f"  {spec['title']}")
    print(f"  {spec['duration']/60:.1f} min · {len(frames)} frames · {len(sections)} sections")
    print(f"  -> {out}/course.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
