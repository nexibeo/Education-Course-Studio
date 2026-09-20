#!/usr/bin/env python3
"""
The question layer.

Two outcomes, decided by the model against the course transcript:

  covered      the answer is already in the video -> reply in chat with the
               section and timestamp, and a seek link. No slide is made,
               because re-teaching something the learner already sat through
               is worse than pointing at it.

  uncovered    the video never answers it -> author a new slide, append it to
               the END of the section the question was asked from, and tell
               the learner it is waiting there.

The judgement is deliberately one LLM call with the whole transcript in the
prompt: 11.7k characters for a ten-minute course is small, and retrieval over
a corpus this size loses more to chunking than it gains.

Generated slides go in `course.json:generated[]`, never into `frames[]`, so
re-ingesting the source build never destroys learner-authored additions.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
import wave
from pathlib import Path

MODEL = os.environ.get("CS_MODEL", "deepseek/deepseek-v4-pro")
KOKORO = os.environ.get("CS_TTS", "http://127.0.0.1:8880/v1/audio/speech")
VOICE = os.environ.get("CS_VOICE", "bm_george")
# Two rounds is enough to turn "how about rate sheet?" into something
# answerable. More than that and the learner is being interrogated.
MAX_CLARIFY = 2
API = "https://openrouter.ai/api/v1/chat/completions"


def _key() -> str:
    k = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not k:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    return k


def mmss(seconds: float) -> str:
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}:{s:02d}"


def call_llm(system: str, user: str, max_tokens: int = 2000) -> str:
    body = {
        "model": MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        API,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.loads(r.read())
    return (d["choices"][0]["message"].get("content") or "").strip()


def parse_json(text: str) -> dict | None:
    """Models wrap JSON in prose or fences more often than not."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S)
    try:
        return json.loads(t)
    except Exception:
        pass
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def transcript_for_prompt(course: dict) -> str:
    """Numbered, timestamped narration — the model cites by beat number."""
    lines = []
    by_section: dict[str, list] = {}
    for b in course["beats"]:
        by_section.setdefault(b["section_id"], []).append(b)
    for s in course["sections"]:
        lines.append(f"\n## Section {s['num']} — {s['title']}  [{s['at']}]")
        for b in by_section.get(s["id"], []):
            lines.append(f"[beat {b['n']} @ {b['at']}] {b['vo']}")
    return "\n".join(lines)


ASK_SYSTEM = """You answer a learner's question about a course they are watching.

You are given the full narration transcript, numbered by beat with timestamps,
and the exchange so far.

Answer with JSON only. No prose, no code fences. Choose ONE of three:

1. The question is too vague to answer well. Ask for what you need:
{"clarify": "<one short question back, max 20 words>",
 "options": ["<2-4 likely readings of their question, each max 8 words>"]}

Use this when the question is a bare topic ("how about rate sheet?"), when it
could mean several different things, or when the useful answer depends on
their situation. Ask about what they want to DO, not about definitions. Never
ask more than one thing at a time. Do not use this if the question is already
specific enough to answer.

2. The transcript genuinely answers it:
{"covered": true, "beat": <beat number>, "why": "<one sentence, max 25 words, saying what the course says>"}

Pick the EARLIEST beat that actually answers it. A beat that merely mentions
the topic in passing is not an answer.

3. It is not answered, and you now understand the question well enough:
{"covered": false,
 "slide": {
   "type": "<statement | list>",
   "kicker": "<2-4 word label>",
   "title": "<short headline, max 9 words>",
   "body": "<for statement: 2-3 sentences that actually ANSWER them, max 55 words>",
   "items": ["<for list: 3-5 short steps or points, max 10 words each>"]
 },
 "vo": "<the narration for this slide: 3-5 sentences, spoken register, that teach the answer. This is what they will HEAR, so it must stand alone and be genuinely useful, not a summary of the slide.>"}

The slide must HELP. Give the actual answer, the actual steps, the actual
guidance. A slide that only says "this is out of scope" is a failure unless
the question is truly unrelated to the course, and even then say where they
should look instead.

Style rules, non-negotiable:
- The audience is a working professional, not a developer. No jargon.
- Never use an em dash. Use a full stop or a comma.
- Banned words: delve, leverage, seamless, unlock, robust, elevate, empower,
  streamline, harness, revolutionize, supercharge, cutting-edge, transformative,
  game changer, dive in, realm, landscape, myriad, foster.
- Narration is spoken: short sentences, contractions, no bullet syntax."""


def synth_vo(text: str, dest: Path) -> float | None:
    """Narrate a generated slide. Returns duration, or None if TTS is down."""
    try:
        body = json.dumps({"model": "kokoro-v1", "input": text, "voice": VOICE, "speed": 1.0}).encode()
        req = urllib.request.Request(KOKORO, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(r.read())
        with wave.open(str(dest)) as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        # A slide without narration still teaches. Losing the answer because
        # the voice server is down would not.
        return None


def ask(
    course: dict,
    question: str,
    section_id: str | None = None,
    history: list | None = None,
    course_dir: Path | None = None,
) -> dict:
    """
    Returns a chat-shaped result:
      {kind: 'clarify', text, options[]}
      {kind: 'covered', text, section, at, seek}
      {kind: 'slide',   text, slide, section_id}
      {kind: 'error',   text}

    `history` is the exchange so far, oldest first, as
    [{"role": "user"|"assistant", "text": ...}]. The client owns it, so the
    server holds no per-learner state.
    """
    q = (question or "").strip()
    if not q:
        return {"kind": "error", "text": "Ask a question and I will check the course for you."}

    hist = history or []
    asked_already = sum(1 for h in hist if h.get("role") == "assistant" and h.get("clarify"))

    convo = ""
    if hist:
        convo = "\n\nEXCHANGE SO FAR:\n" + "\n".join(
            f"{'Learner' if h.get('role') == 'user' else 'You'}: {h.get('text','')}" for h in hist
        )

    budget = ""
    if asked_already >= MAX_CLARIFY:
        budget = (
            "\n\nYou have already asked for clarification twice. Do NOT ask again. "
            "Answer with what you have, choosing covered or a slide."
        )

    user = (
        f"COURSE: {course['title']}\n"
        f"TRANSCRIPT:\n{transcript_for_prompt(course)}"
        f"{convo}\n\nLEARNER QUESTION: {q}{budget}"
    )
    try:
        raw = call_llm(ASK_SYSTEM, user, max_tokens=2600)
    except Exception as ex:
        return {"kind": "error", "text": f"Could not reach the model: {ex}"}

    data = parse_json(raw)
    if not data:
        return {"kind": "error", "text": "The model did not return usable JSON. Try rephrasing."}

    # --- 1. needs clarifying -------------------------------------------
    if data.get("clarify") and asked_already < MAX_CLARIFY:
        opts = [str(o) for o in (data.get("options") or [])][:4]
        return {"kind": "clarify", "text": str(data["clarify"]).strip(), "options": opts}

    # --- 2. already in the video ---------------------------------------
    if data.get("covered"):
        beat = next((b for b in course["beats"] if b.get("n") == data.get("beat")), None)
        if beat:
            sec = next((s for s in course["sections"] if s["id"] == beat["section_id"]), None)
            why = (data.get("why") or "").strip()
            label = f"Section {sec['num']} — {sec['title']}" if sec else "the course"
            return {
                "kind": "covered",
                "text": f"{why} That is in {label}, at {beat['at']}.",
                "section": label,
                "at": beat["at"],
                "seek": beat["start"],
            }
        # Named a beat that does not exist: fall through and author a slide
        # rather than cite a timestamp that goes nowhere.

    # --- 3. author the answer ------------------------------------------
    slide = data.get("slide") or {}
    if not slide.get("title"):
        return {"kind": "error", "text": "I could not compose an answer for that. Try rephrasing."}

    sid = section_id or (course["sections"][0]["id"] if course["sections"] else None)
    sec = next((s for s in course["sections"] if s["id"] == sid), None)
    gid = f"g{len(course.get('generated', [])) + 1}"
    vo = (data.get("vo") or "").strip()

    entry = {
        "id": gid,
        "section_id": sid,
        "question": q,
        "type": slide.get("type") or "statement",
        "kicker": slide.get("kicker") or "Your question",
        "title": slide.get("title"),
        "body": slide.get("body") or "",
        "items": slide.get("items") or [],
        "vo": vo,
        "audio": None,
        "dur": None,
    }

    # Narrate it. The learner asked to be taught, not handed a card to read.
    if vo and course_dir:
        dest = Path(course_dir) / "generated" / f"{gid}.wav"
        dur = synth_vo(vo, dest)
        if dur:
            entry["audio"] = f"{gid}.wav"
            entry["dur"] = round(dur, 2)

    label = f"Section {sec['num']} — {sec['title']}" if sec else "this section"
    spoken = " It is narrated, so it will play when you reach it." if entry["audio"] else ""
    return {
        "kind": "slide",
        "text": f"The course does not cover that, so I have added a slide at the end of {label}.{spoken}",
        "slide": entry,
        "section_id": sid,
    }
