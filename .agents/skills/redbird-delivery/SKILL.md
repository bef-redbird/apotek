---
name: redbird-delivery
description: Prepare bef-redbird dependency, pull request and branch-cleanup delivery.
---

Inspect origin, branch, worktrees and diff. Run locked Ruff, mypy, synthetic
pytest, browser and tracked/staged hygiene gates; do not claim skipped checks.
Document behaviour, data impact, actual validation and remaining host/private
acceptance in the PR. Do not bypass review or CI to merge. Delete a merged
branch only after checking its exact head, PR base, dependent work and worktree
use. Report retained branches and pending administrative actions.
