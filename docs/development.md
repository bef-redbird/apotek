# Development and delivery

Use Python 3.12 and uv from the repository root. The committed `pyproject.toml`
and `uv.lock` define the environment. Never commit private inputs or generated
workbooks; generate synthetic fixtures in test temporary directories.

```text
uv sync --locked --group dev --group browser
uv run pre-commit install --hook-type pre-commit --hook-type pre-push
uv run ruff check .
uv run ruff format --check .
uv run mypy src/bef_redbird
uv run pytest -m "not browser and not private"
uv run playwright install chromium
uv run pytest -m browser tests/browser
uv run python scripts/check_repo_hygiene.py --tracked
git diff --check
```

On Linux CI, install browser OS dependencies using `playwright install --with-deps
chromium`. Browser tests use generated synthetic workbooks. Private checks are
opt-in and need authorised external file paths; never provide them to public CI.
Do not describe skipped private-file, full-size or hosted-access checks as passed.

Pre-commit runs pinned Ruff, whitespace/YAML/conflict, detect-secrets (including
private-key detection) and large-file
checks plus the staged-content guard. Pre-push runs deterministic synthetic tests
and type checking. Hooks do not auto-stage fixes, merge, publish or delete branches.
CI runs the authoritative locked checks, Chromium workflow and Windows smoke.
No Codex lifecycle hook configuration is installed: client support/trust has not
been verified. CI actions use full upstream commit SHAs with tag comments;
dependency updates still require compatibility testing and diff review.

The hygiene guard reads Git index blobs, including when the working-tree file
differs. `--staged` checks added/changed index content; `--tracked` checks the entire
index. It rejects generated/private paths, workbook/binary artifacts, large files,
private keys and tabular source-header signatures hidden in text files. Failures
print reason codes rather than filenames, secret values or source rows. Review
the local diff to identify rejected files. This heuristic cannot prove privacy:
inspect every staged diff and never use the guard as permission to upload data.
Authored configuration, Markdown and small synthetic fixture recipes are allowed.
Intentional new artifact types require a reviewed narrow policy change.

Before a PR, inspect origin, status, staged and unstaged diffs, branch and worktrees.
Run the relevant checks and record factual results in the PR template. Explain
material method changes, data impact and any migration/rollback. Do not self-approve
through another identity, bypass required checks or force an administrative merge.
Repository protection/private access are administrative gates, not settings this
workflow claims to configure. Use squash merge when repository policy permits.

## Safe branch cleanup

Cleanup starts only after confirmed merge, not after auto-merge is scheduled.
These commands inspect state; run from this verified repository:

```text
git remote -v
git status --short
git worktree list --porcelain
git branch --show-current
gh repo view --json nameWithOwner,defaultBranchRef
git fetch --prune origin
gh pr view <number> --json state,mergedAt,baseRefName,headRefName,headRefOid
git rev-parse <branch>
git ls-remote --heads origin refs/heads/<branch>
gh pr list --state open --json number,baseRefName,headRefName
```

Fetch is an explicit network action; stop safely on authentication/offline errors.
Verify the matching PR belongs to this repository and merged into the intended
default base. Retain default/protected/long-lived branches, the current branch,
every worktree branch, branches with open/stacked dependants and branches whose
tip differs from the PR's merged head. Remote absence does not prove local work
is disposable. Inspect dirty work and never reset, clean or remove worktrees as
a cleanup shortcut.

After verifying the remote tip equals the expected merged head, remove only that
remote ref with an expected-value lease (replace placeholders explicitly):

```text
git push --force-with-lease=refs/heads/<branch>:<expected-head-sha> origin :refs/heads/<branch>
git branch -d <branch>
git fetch --prune origin
```

Switch to the default branch before local deletion, after protecting any dirty
work. Squash merging can make `branch -d` refuse even genuinely merged work.
Use a narrowly scoped `git branch -D <branch>` only after independently verifying
the merged PR, unchanged exact head, no worktree use and no dependent or unique
unpreserved work. Never use blanket deletion pipelines. An obsolete unmerged
branch additionally needs an explicit abandoned/superseded decision; age is not
evidence. Recheck local/remote refs and report removed/retained branches and
reasons to the operator, without creating repository cleanup logs.
