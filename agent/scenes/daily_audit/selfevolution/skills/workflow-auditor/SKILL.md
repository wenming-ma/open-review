---
name: workflow-auditor
description: Audit one selected action workflow end to end and escalate only bounded, evidence-backed autofix candidates.
---

# Workflow Auditor

- Audit exactly one selected action workflow.
- Follow the workflow from the user-facing entrypoint through the relevant code paths rather than analyzing isolated helpers out of context.
- Converge on one primary issue for the run and go deep on it.
- Do not enumerate multiple unrelated findings in one pass.
- Prefer one strong issue with a complete evidence chain over several shallow findings.
- Use repository evidence and local experiments when they materially increase confidence.
- For performance or optimization concerns, prefer an actual script, harness, or benchmark over static reasoning alone.
- If you cannot produce empirical evidence for a performance or optimization claim, keep it out of formal findings and leave it as a suspicion or open question in narrative text.
- Stay `report_only` unless the issue is bounded, low-blast-radius, and verification is clear.
- Prefer small, practical fixes over broad refactors or speculative rewrites.
