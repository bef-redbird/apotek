# Repository instructions

Read CODEX_HANDOVER.md before changing behaviour. Preserve its safeguards and
independently verify proposed choices. Version changed method definitions and
update their tests and explanations together. Inspect origin, branch, worktrees
and existing changes before editing; the intended repository is bef-redbird.

Keep this release lean: Danish Streamlit review workspace, deterministic
analysis, Excel import/export, temporary sessions. No models, clinical advice,
automatic orders or speculative infrastructure. Editable N (initially 50) and
all-rankable are view preferences: preserve analysis identity, ranks and reviews.

Cito requires only varenr, opbevkode, kostpris, subkode, nysubkode and subgrp.
Use LMS's data sheet and exact AIP rows. Preserve IDs, missingness, integer-øre
prices, source versions and decision-time boundaries. Upcoming C is not proof
of an own-price rise. Groups describe the current report only.

Use .agents/skills/redbird-data-integrity/SKILL.md for calculation/data changes,
redbird-ui-review for UI changes and redbird-delivery for delivery. Run targeted
tests while editing, all required gates before handoff, and report actual checks.
Never rewrite expectations simply to accept an incorrect result.

Private workbooks are read-only external fixtures, never repository assets.
No source/derived data, notes, databases, logs, screenshots, secrets or agent
histories in Git. Synthetic recipes are the default test inputs. Do not print
private rows or paths in diagnostics or send data to new services.

Use focused PRs and do not remove user changes. Clean merged branches only
after verifying merged PR, unchanged head, worktree use and dependencies.
Never bulk-force-delete branches or auto-merge through hooks.
