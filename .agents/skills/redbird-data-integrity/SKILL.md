---
name: redbird-data-integrity
description: Change bef-redbird imports, financial calculations, ranking or exports while preserving source and decision-time contracts.
---

Read the affected contract in CODEX_HANDOVER.md. Encode independently expected
examples before changing semantics: cent boundaries, missing slots, exact 5%
rises and observable reversal denominators. Verify both source formats without
requiring optional fields. Test future-data exclusion and deterministic full
ranking separately from queue slicing. Compare Excel readback with the same
AnalysisResult and test formula injection and matching-only JSON restore.
Use external authorised originals read-only; default tests generate synthetic
workbooks. Report exact checks and unavailable private evidence.
