"""The project side: a hardware project whose history records how hard you were
thinking when you changed it.

Every contribution carries the effort score measured at the moment it was
written, plus whether the author deliberately flagged it. Downstream, the agent
reads those scores instead of treating every line in the history as equally
considered -- which is the whole point. A BOM swap you made at 3am while
tab-completing is not the same evidence as one you flagged and defended.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import asdict, dataclass, field
from threading import Lock

# Below this, the agent challenges you instead of executing.
LOW_EFFORT_THRESHOLD = 35.0
# At or above this, input is treated as considered.
HIGH_EFFORT_THRESHOLD = 65.0


def classify_effort(effort: float | None, flagged: bool = False) -> str:
    if flagged:
        return "flagged"
    if effort is None:
        return "unknown"
    if effort < LOW_EFFORT_THRESHOLD:
        return "diffuse"
    if effort >= HIGH_EFFORT_THRESHOLD:
        return "focused"
    return "neutral"


def confidence_weight(effort: float | None, flagged: bool = False) -> float:
    """How much the agent should trust this contribution, 0-1.

    A deliberate flag floors the weight at 0.95: overriding the meter by choice
    is itself a strong signal, and a system that ignored an explicit human flag
    because a dry electrode drifted would deserve everything it got.
    """
    if flagged:
        return 0.95
    if effort is None:
        return 0.5
    return round(min(1.0, max(0.05, effort / 100.0)), 3)


@dataclass
class Artifact:
    """A file in the project. Hardware projects are mostly not source code."""

    id: str
    name: str
    kind: str          # step | gerber | firmware | sim | bom | doc | image
    summary: str
    version: int = 1
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Variant:
    """A physical design option sitting on the table.

    Prism knows where each one is; `position` is its left-to-right placement on
    [-1, +1] so the attention tracker can map gaze onto it. `attention_seconds`
    accumulates real dwell time, which is the interesting artifact: at the end of
    a session you know which option the team actually considered, not just which
    one they said they liked.
    """

    id: str
    label: str
    position: float
    summary: str
    version: int = 1
    status: str = "candidate"      # candidate | promoted | archived
    attention_seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["attention_seconds"] = round(self.attention_seconds, 1)
        return data


@dataclass
class Contribution:
    """One entry in the project history, scored by the author's measured state."""

    id: str
    author: str
    body: str
    kind: str                     # decision | note | change | question | agent
    effort: float | None
    flagged: bool
    weight: float
    label: str
    status: str                   # proposed | merged | challenged | rejected
    created_at: float
    artifact_id: str | None = None
    agent_response: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class ProjectStore:
    """In-memory project state. Single process, one project, hackathon scope."""

    def __init__(self):
        self._lock = Lock()
        self._ids = itertools.count(1)
        self.name = "Thermal Camera Rev C"
        self.description = (
            "Handheld thermal imager. Lepton 3.5 core, custom carrier PCB, "
            "injection-moulded enclosure."
        )
        self.contributions: list[Contribution] = []
        self.artifacts: list[Artifact] = []
        self.variants: list[Variant] = []
        self.flag_events: list[dict] = []
        self._seed()

    def _next_id(self, prefix: str) -> str:
        return f"{prefix}_{next(self._ids):04d}"

    def _seed(self) -> None:
        for name, kind, summary in [
            ("enclosure_rev_c.step", "step", "Two-part ABS shell, 118 x 64 x 31 mm, M2 bosses."),
            ("carrier_board.kicad_pcb", "gerber", "4-layer carrier, Lepton socket, USB-C PD."),
            ("thermal_sim.inp", "sim", "Steady-state thermal, 2.1 W core dissipation."),
            ("bom.csv", "bom", "41 line items, 3 single-sourced."),
            ("firmware/main.c", "firmware", "STM32H7 capture loop, 9 Hz frame rate."),
        ]:
            self.artifacts.append(
                Artifact(id=self._next_id("art"), name=name, kind=kind, summary=summary)
            )

        # Two printed shells on the table, left and right of centre.
        for label, position, summary in [
            ("Shell A", -0.65, "Wraparound grip, 31 mm thick, single-shot mould, 2 mm walls."),
            ("Shell B", 0.65, "Slab back with vented fin, 27 mm thick, side-action tool required."),
        ]:
            self.variants.append(
                Variant(id=self._next_id("var"), label=label, position=position, summary=summary)
            )

    def variant(self, variant_id: str) -> Variant | None:
        return next((v for v in self.variants if v.id == variant_id), None)

    def zones(self) -> dict[str, float]:
        return {v.id: v.position for v in self.variants if v.status != "archived"}

    def add_attention(self, variant_id: str, seconds: float) -> None:
        with self._lock:
            v = self.variant(variant_id)
            if v is not None:
                v.attention_seconds += seconds

    def promote_variant(self, variant_id: str) -> Variant | None:
        with self._lock:
            target = self.variant(variant_id)
            if target is None:
                return None
            for v in self.variants:
                v.status = "promoted" if v.id == variant_id else "archived"
            target.version += 1
            return target

    # ----------------------------------------------------------- mutation

    def add_contribution(
        self,
        author: str,
        body: str,
        kind: str,
        effort: float | None,
        flagged: bool,
        artifact_id: str | None = None,
        status: str = "proposed",
    ) -> Contribution:
        with self._lock:
            contribution = Contribution(
                id=self._next_id("c"),
                author=author,
                body=body,
                kind=kind,
                effort=effort,
                flagged=flagged,
                weight=confidence_weight(effort, flagged),
                label=classify_effort(effort, flagged),
                status=status,
                created_at=time.time(),
                artifact_id=artifact_id,
            )
            self.contributions.append(contribution)
            return contribution

    def set_status(self, contribution_id: str, status: str) -> Contribution | None:
        with self._lock:
            for c in self.contributions:
                if c.id == contribution_id:
                    c.status = status
                    return c
            return None

    def attach_agent_response(self, contribution_id: str, response: str) -> None:
        with self._lock:
            for c in self.contributions:
                if c.id == contribution_id:
                    c.agent_response = response
                    return

    def record_flag(self, event: dict) -> dict:
        with self._lock:
            enriched = {"id": self._next_id("flag"), **event}
            self.flag_events.append(enriched)
            return enriched

    def add_artifact(self, name: str, kind: str, summary: str) -> Artifact:
        with self._lock:
            artifact = Artifact(id=self._next_id("art"), name=name, kind=kind, summary=summary)
            self.artifacts.append(artifact)
            return artifact

    # ------------------------------------------------------------ queries

    def merged(self) -> list[Contribution]:
        return [c for c in self.contributions if c.status == "merged"]

    def stats(self) -> dict:
        total = len(self.contributions)
        merged = len(self.merged())
        scored = [c for c in self.contributions if c.effort is not None]
        avg = round(sum(c.effort for c in scored) / len(scored), 1) if scored else None
        return {
            "totalContributions": total,
            "merged": merged,
            "challenged": len([c for c in self.contributions if c.status == "challenged"]),
            "flagged": len([c for c in self.contributions if c.flagged]),
            "averageEffort": avg,
            "artifacts": len(self.artifacts),
        }

    def history_for_agent(self, limit: int = 40) -> str:
        """The project history, rendered so the agent can weigh it.

        Contributions are ordered oldest-first and annotated with the author's
        measured state, because that annotation is the thing the agent is
        supposed to reason over.
        """
        lines = [f"PROJECT: {self.name}", f"DESCRIPTION: {self.description}", "", "ARTIFACTS:"]
        for a in self.artifacts:
            lines.append(f"  - {a.name} ({a.kind}, v{a.version}): {a.summary}")

        lines.append("")
        lines.append("DESIGN VARIANTS ON THE TABLE (attention = measured dwell time):")
        for v in self.variants:
            lines.append(
                f"  - [{v.id}] {v.label} (v{v.version}, {v.status}): {v.summary}"
                f" · attention {v.attention_seconds:.0f}s"
            )
            for note in v.notes:
                lines.append(f"      note: {note}")

        lines.append("")
        lines.append("HISTORY (oldest first; effort 0-100 measured from EEG at time of writing):")
        recent = self.contributions[-limit:]
        if not recent:
            lines.append("  (empty)")
        for c in recent:
            effort = "n/a" if c.effort is None else f"{c.effort:.0f}"
            flag = " [FLAGGED BY AUTHOR]" if c.flagged else ""
            lines.append(
                f"  [{c.id}] {c.author} · {c.kind} · effort={effort} ({c.label})"
                f" · weight={c.weight} · {c.status}{flag}"
            )
            lines.append(f"      {c.body}")
            if c.agent_response:
                lines.append(f"      agent: {c.agent_response}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "artifacts": [a.to_dict() for a in self.artifacts],
            "variants": [v.to_dict() for v in self.variants],
            "contributions": [c.to_dict() for c in self.contributions],
            "stats": self.stats(),
            "thresholds": {"low": LOW_EFFORT_THRESHOLD, "high": HIGH_EFFORT_THRESHOLD},
        }
