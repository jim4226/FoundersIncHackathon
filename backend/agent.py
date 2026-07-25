"""The agent that reads your effort level before it reads your prompt.

The gate is the product. Three regimes:

  diffuse   Measured effort below the low threshold and no deliberate flag. The
            agent refuses to touch the project and asks one pointed question
            instead. This is the demo's whole argument: the same sentence gets a
            different response depending on the state of the person who typed it.
  neutral   It acts, but marks the change for review.
  focused   Effort above the high threshold, or the author double-blinked to
            flag it. Treated as considered intent and merged.

A flag always beats a low reading. The human overriding the meter on purpose is
a stronger signal than the meter.
"""

from __future__ import annotations

import os
import re

from .store import (
    HIGH_EFFORT_THRESHOLD,
    LOW_EFFORT_THRESHOLD,
    ProjectStore,
    classify_effort,
)

MODEL = os.environ.get("AGENT_MODEL", "claude-sonnet-5")
MAX_TOKENS = 900

SYSTEM_PROMPT = """You are the engineering agent for a hardware project. You have \
the project's full history: CAD, PCB, firmware, simulation and BOM artifacts, plus \
every decision anyone has recorded.

What makes you unusual: each contribution in the history carries an `effort` score \
from 0-100, measured from the author's EEG at the moment they wrote it, and a \
`weight` derived from it. High-effort and author-flagged contributions are considered \
intent. Low-effort ones were typed while the author was diffuse -- half-attending, \
tab-completing, tired. Weigh the history accordingly. When two entries conflict, the \
higher-weighted one generally wins, and you should say so explicitly when it matters.

You will be told the current author's measured state with each request. Follow the \
regime you are given:

- DIFFUSE: Do not modify the project. Reply in 2-3 sentences: name what is \
underspecified about their request, then ask exactly ONE concrete question that would \
let you act. Be direct and collegial, never scolding, never preachy. Do not mention \
brainwaves or lecture them about focus -- just ask the question a good engineer would \
ask. End with a short note that they can double-blink to flag the request through as-is.
- REVIEW: Act on the request, but state the one assumption you are least sure of.
- CONSIDERED: Act on the request directly and concisely. State what changed and the \
consequence for the build.

Be specific and technical. Reference real artifacts and part numbers from the history. \
Never pad. Engineers are reading this."""

ANSWER_PROMPT = """You are the engineering agent for a hardware project, answering \
questions about it. You have the full record: CAD, PCB, firmware, simulation and BOM \
artifacts, the design variants on the bench, and every decision anyone recorded.

Each decision carries an `effort` score from 0-100, measured from the author's EEG at \
the moment they wrote it, and each design variant carries `attention` -- the measured \
seconds the team actually spent looking at it. Use both. When you summarise, say which \
decisions were considered and which were made while the author was diffuse, and which \
option genuinely got looked at rather than merely talked about. That distinction is the \
whole reason this record is worth more than a chat log.

Answer directly and concretely, in a few short paragraphs. Do not modify anything -- \
this is a read. Do not pad or flatter. Engineers are reading this."""


def _regime(effort: float | None, flagged: bool) -> str:
    label = classify_effort(effort, flagged)
    if label == "flagged" or label == "focused":
        return "CONSIDERED"
    if label == "diffuse":
        return "DIFFUSE"
    return "REVIEW"


class ProjectAgent:
    """Wraps Claude, degrading to a deterministic responder when unconfigured.

    The fallback is not a stub for its own sake: at a hackathon the network dies,
    the key is unset, or the venue blocks outbound TLS, and the gate still has to
    be demonstrable on stage. The fallback exercises the identical branch logic.
    """

    def __init__(self, store: ProjectStore):
        self.store = store
        self._client = None
        self.mode = "offline"
        key = os.environ.get("ANTHROPIC_API_KEY")
        if key:
            try:
                import anthropic

                self._client = anthropic.Anthropic(api_key=key)
                self.mode = "live"
            except Exception:  # noqa: BLE001 - offline mode is a valid state
                self._client = None
                self.mode = "offline"

    def answer(self, question: str) -> dict:
        """Read the project and answer. Never writes, never gated.

        Talking to the project is the read path, and reads are not gated on
        effort: someone who has lost the thread asking "walk me through this" is
        the system working, not a lapse to be challenged. Nor does the question
        become a project decision -- recording every question as a change would
        corrupt the exact history this is all built to keep.
        """
        if self._client is not None:
            try:
                text = self._answer_live(question)
            except Exception as exc:  # noqa: BLE001 - never break the demo
                text = self._answer_offline(question) + f"\n\n_(offline fallback: {type(exc).__name__})_"
        else:
            text = self._answer_offline(question)
        return {"regime": "ANSWER", "text": text, "acted": False,
                "effort": None, "flagged": False}

    def _answer_live(self, question: str) -> str:
        response = self._client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=ANSWER_PROMPT,
            messages=[{"role": "user", "content":
                       f"{self.store.history_for_agent()}\n\nQuestion: {question}"}],
        )
        return "".join(b.text for b in response.content if b.type == "text").strip()

    def _answer_offline(self, question: str) -> str:
        stats = self.store.stats()
        lines = [
            f"{self.store.name} — {self.store.description}", "",
            f"{stats['totalContributions']} recorded decisions, {stats['merged']} merged, "
            f"{stats['challenged']} challenged. Average operator effort {stats['averageEffort']}.",
            "",
            "Designs on the table:",
        ]
        for v in self.store.variants:
            lines.append(
                f"  · {v.label} (v{v.version}, {v.status}) — "
                f"{v.attention_seconds:.0f}s of measured attention. {v.summary}"
            )
        considered = max(self.store.variants, key=lambda v: v.attention_seconds, default=None)
        if considered and considered.attention_seconds > 0:
            lines += ["", f"Most considered: {considered.label}, by measured dwell."]
        return "\n".join(lines)

    def respond(self, message: str, effort: float | None, flagged: bool) -> dict:
        regime = _regime(effort, flagged)
        if self._client is not None:
            try:
                text = self._respond_live(message, effort, flagged, regime)
            except Exception as exc:  # noqa: BLE001 - never break the demo
                text = self._respond_offline(message, regime)
                text += f"\n\n_(offline fallback: {type(exc).__name__})_"
        else:
            text = self._respond_offline(message, regime)

        return {
            "regime": regime,
            "text": text,
            "acted": regime != "DIFFUSE",
            "effort": effort,
            "flagged": flagged,
        }

    def _respond_live(self, message: str, effort: float | None, flagged: bool, regime: str) -> str:
        effort_str = "unmeasured" if effort is None else f"{effort:.0f}/100"
        context = self.store.history_for_agent()
        prompt = (
            f"{context}\n\n"
            f"--- CURRENT REQUEST ---\n"
            f"Author's measured effort: {effort_str}\n"
            f"Author flagged this deliberately: {'yes' if flagged else 'no'}\n"
            f"Regime: {regime}\n\n"
            f"Request: {message}"
        )
        response = self._client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in response.content if block.type == "text").strip()

    def _respond_offline(self, message: str, regime: str) -> str:
        """Deterministic stand-in that still branches on the gate."""
        subject = self._subject(message)
        if regime == "DIFFUSE":
            return (
                f"Holding off on {subject} — the request is underspecified as written, "
                f"and there are three merged decisions it would contradict.\n\n"
                f"One question before I touch anything: what is the constraint driving "
                f"this — thermal, cost, or lead time?\n\n"
                f"If you meant it as stated, double-blink to flag it and I'll apply it as-is."
            )
        if regime == "REVIEW":
            return (
                f"Applied to {subject} and staged for review.\n\n"
                f"Least certain assumption: that this supersedes the earlier decision on "
                f"the same artifact rather than sitting alongside it. Flag it if that's wrong."
            )
        return (
            f"Done — {subject} updated and merged.\n\n"
            f"Consequence for the build: this touches the thermal path, so "
            f"`thermal_sim.inp` is now stale and the BOM has one fewer single-sourced line."
        )

    @staticmethod
    def _subject(message: str) -> str:
        cleaned = re.sub(r"\s+", " ", message).strip().rstrip(".")
        if not cleaned:
            return "the project"
        words = cleaned.split(" ")
        return " ".join(words[:8]).lower() + ("…" if len(words) > 8 else "")


__all__ = ["ProjectAgent", "LOW_EFFORT_THRESHOLD", "HIGH_EFFORT_THRESHOLD"]
