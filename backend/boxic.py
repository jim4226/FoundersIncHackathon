"""Adapter onto Boxic — the living project record for hardware.

Boxic stores a whole project as one JSON document in `workspace_projects.data`,
and the two shapes Bench cares about are:

    Version         { id, number, authorHandle, createdAt, commitMessage,
                      sourceFileName, status, reviews, ... }
    ProjectDecision { id, createdAt, authorHandle, versionId, title, body,
                      origin: "conversation" | "manual" }

That `origin` field is the seam. Boxic already distinguishes a decision reached
by talking to the project from one typed in by hand; Bench is simply a third
origin — a decision reached at the physical bench, with the operator's measured
state attached.

Because `data` is jsonb, the extra keys Bench adds (`effort`, `weight`,
`attentionSeconds`) ride along without a migration. Anything that reads a
decision today keeps working; anything that wants the state can ask for it. That
is what makes this a small change on Boxic's side rather than a schema argument.

Why attach effort at all: a project history where every entry is weighted by how
considered it was is training data no software repo has. "Walk me through this
project" can then answer with the decisions that were actually deliberated,
instead of averaging them with the ones someone tab-completed at 3am.

Boxic's MCP surface is currently read-only (`list_workspaces`, `list_projects`,
`get_project`), so this module writes by producing Boxic-shaped documents for
import and reads through MCP when credentials are present. When a write tool
lands, `push_decisions` is the only function that changes.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from .store import ProjectStore

BOXIC_ORIGIN = "bench"
BOXIC_BASE_URL = os.environ.get("BOXIC_BASE_URL", "https://boxic.app")


def _decision_id() -> str:
    return f"dec_{uuid.uuid4().hex[:12]}"


def _title_for(contribution) -> str:
    """Boxic decisions carry a short title and a longer body."""
    first = contribution.body.strip().split("\n", 1)[0]
    return first[:77] + "…" if len(first) > 78 else first


def contribution_to_decision(contribution, version_id: str | None = None) -> dict:
    """Map one Bench contribution onto Boxic's ProjectDecision shape."""
    effort = contribution.effort
    body = contribution.body.strip()
    if contribution.agent_response:
        body += f"\n\nAgent: {contribution.agent_response}"

    # The provenance line is written into the body as well as the structured
    # fields, so a reader who never learns about the extra keys still sees how
    # the decision was reached.
    state = "flagged deliberately" if contribution.flagged else contribution.label
    measured = "unmeasured" if effort is None else f"{effort:.0f}/100"
    body += f"\n\n— recorded at the bench · operator effort {measured} ({state})"

    return {
        "id": _decision_id(),
        "createdAt": int(contribution.created_at * 1000),
        "authorHandle": contribution.author,
        "versionId": version_id,
        "title": _title_for(contribution),
        "body": body,
        "origin": BOXIC_ORIGIN,
        # Extra keys, carried by jsonb without a migration.
        "effort": effort,
        "weight": contribution.weight,
        "flagged": contribution.flagged,
        "benchStatus": contribution.status,
    }


# Bench's variant lifecycle onto Boxic's VersionStatus. Boxic's enum is
# working | in_review | changes_requested | approved | released | superseded |
# archived -- there is no "draft", so anything outside this map would be an
# invalid status on import. `redesign` lands on `changes_requested`, which is
# exactly what a considered thumbs-down means.
VERSION_STATUS = {
    "promoted": "approved",
    "archived": "archived",
    "redesign": "changes_requested",
    "candidate": "working",
}
DEFAULT_VERSION_STATUS = "working"


def variant_to_version(variant, number: int) -> dict:
    """Map a design variant on the table onto a Boxic Version.

    `attentionSeconds` is the interesting addition: measured dwell time, so the
    record shows which option the team actually considered rather than only
    which one they ended up saying they liked.
    """
    return {
        "id": f"ver_{variant.id}",
        "number": number,
        "authorHandle": "bench",
        "createdAt": int(time.time() * 1000),
        "commitMessage": f"{variant.label} — {variant.summary}",
        "sourceFileName": f"{variant.label.lower().replace(' ', '_')}.step",
        "sourceMimeType": "model/step",
        "portrait": None,
        "status": VERSION_STATUS.get(variant.status, DEFAULT_VERSION_STATUS),
        "reviews": [],
        "attentionSeconds": round(variant.attention_seconds, 1),
        "benchStatus": variant.status,
    }


def export_document(store: ProjectStore) -> dict:
    """Render the whole bench session as a Boxic-shaped project document.

    Suitable for dropping into `workspace_projects.data`, or for diffing against
    what `get_project` returns.
    """
    return {
        "title": store.name,
        "description": store.description,
        "versions": [variant_to_version(v, i + 1) for i, v in enumerate(store.variants)],
        "decisions": [
            contribution_to_decision(
                c, version_id=f"ver_{c.variant_id}" if c.variant_id else None
            )
            for c in store.contributions
        ],
        "files": [
            {
                "id": a.id,
                "name": a.name,
                "category": a.kind,
                "summary": a.summary,
                "revision": a.version,
            }
            for a in store.artifacts
        ],
        "benchSession": {
            "recordedAt": int(time.time() * 1000),
            "stats": store.stats(),
            "flagEvents": len(store.flag_events),
        },
    }


def write_export(store: ProjectStore, path: str | Path) -> Path:
    """Write the export next to the repo so it can be imported into Boxic."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(export_document(store), indent=2))
    return target


def push_decisions(store: ProjectStore) -> dict:
    """Write decisions into Boxic directly.

    Not yet possible: Boxic's MCP server exposes only `list_workspaces`,
    `list_projects` and `get_project`. Landing this needs one `commit_decision`
    tool on that side taking the ProjectDecision shape above. Until then
    `write_export` produces an importable document, so nothing here blocks.
    """
    raise NotImplementedError(
        "Boxic MCP is read-only (list_workspaces, list_projects, get_project). "
        "Needs a commit_decision tool; use write_export() meanwhile."
    )
