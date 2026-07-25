# Bench collaboration contract

This repository is edited by multiple human and agent sessions. Protect the
live demo by following these rules.

## Before editing

1. Run `git fetch origin --prune`.
2. Start from the current production branch recorded in `PROGRESS.md`.
3. Create one isolated branch per task: `agent/<short-task-name>`.
4. Check open issues and pull requests for overlapping file ownership.
5. Never build on a stale checkout and never force-push a shared branch.

## While working

- Keep one task and one concern per branch.
- Open a draft PR early so the branch and Netlify preview advertise ownership.
- Preserve unrelated changes from other sessions.
- Do not put tokens, credentials, raw EEG, or private project data in the web
  bundle.
- Treat `PROGRESS.md` as milestone-level state, not a per-commit activity log.
  GitHub issues and PRs are the operational source of truth.

## Before pushing

```powershell
git fetch origin --prune
git diff --check
python -m compileall -q backend gesture
node --check web/app.js
node --check web/desk.js
```

If the base branch moved, merge it normally and resolve conflicts before
pushing. Do not rewrite another session's history.

## Pull request handoff

Every PR must include:

- the goal and user-visible impact;
- files intentionally changed;
- checks run and their results;
- the Netlify deploy-preview URL;
- known limitations and the recommended next action.

Production changes are merged only after the preview is visually checked.
