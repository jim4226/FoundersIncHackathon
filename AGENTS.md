# Bench collaboration contract

This repository is edited by multiple human and agent sessions. Protect the
live demo by following these rules.

## Before editing

1. Run `git fetch origin --prune`.
2. Ask GitHub for the current default/integration branch; do not infer it from
   Netlify or a branch-local document.
3. Check open issues and pull requests for overlapping file ownership and
   confirm the intended PR base.
4. Claim one GitHub issue, assign it, and list the files the task expects to
   change. The issue is the ownership lock between sessions.
5. Create a dedicated worktree and isolated branch for the task:
   `agent/<short-task-name>`. Never share a working tree between sessions.
6. Never build on a stale checkout and never force-push a shared branch.

## While working

- Keep one task and one concern per branch.
- Open a draft PR early so GitHub advertises ownership and Netlify can render
  the branch for visual review.
- Preserve unrelated changes from other sessions.
- Do not put tokens, credentials, raw EEG, or private project data in the web
  bundle.
- Treat `PROGRESS.md` as milestone-level state, not a per-commit activity log.
  GitHub issues and PRs are the operational source of truth.
- Treat Netlify as a derived deployment target, never as the record of code,
  task ownership, or project state.
- Before merging a consolidation PR, merge its child PRs into that branch or
  retarget them to the destination so completed work is not stranded.

## Before pushing

```powershell
git fetch origin --prune
git merge --no-edit origin/<base-branch>
git diff --check origin/<base-branch>...HEAD
python -m compileall -q backend gesture desk
Get-ChildItem web -Recurse -Filter *.js | ForEach-Object { node --check $_.FullName }
```

Resolve conflicts and rerun every check after merging an advanced base. Do not
rewrite another session's history.

## Pull request handoff

Every PR must include:

- the goal and user-visible impact;
- the confirmed base branch and any overlapping PRs;
- files intentionally changed;
- checks run and their results;
- the Netlify deploy-preview URL;
- known limitations and the recommended next action.

Changes reach the deployed branch only after the preview is visually checked.
