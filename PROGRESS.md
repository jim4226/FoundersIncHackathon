# Bench progress

Last reviewed: 2026-07-24

## Live surfaces

- Production: https://foundersinchacknight.netlify.app
- GitHub: https://github.com/jim4226/FoundersIncHackathon
- Current production branch: `claude/camera-eeg-hackathon-6v864b`
- Target stable production branch: `main` after PR #1 is verified and merged

## Product focus

Hardware teams version files but lose the why behind physical design decisions.
Bench captures what was considered and approved at the workbench and turns it
into traceable, agent-readable project history.

## Milestones

| Milestone | Status | Exit condition |
|---|---|---|
| Hosted progress/replay demo | In progress | Netlify `/` loads and the demo works without local hardware |
| Local sensor demo | Working prototype | Muse or simulator, gesture votes, and UI run together reliably |
| Durable decision record | Next | Decisions survive restarts and appear immediately in the UI |
| Boxic write integration | Blocked | A supported Boxic write surface is available |
| Real-user validation | Planned | Results from multiple Muse sessions are recorded |

## Current constraints

- Netlify can host the browser demo, but not the persistent FastAPI WebSocket
  process or local Muse/webcam hardware.
- The hardware backend remains local until it is moved to an always-on service
  with persistent storage.
- Operational task ownership lives in GitHub issues and draft PRs so parallel
  sessions do not compete over this file.

## Session handoff

The next session should read `AGENTS.md`, fetch the remote, inspect open PRs, and
claim a non-overlapping task before editing.
