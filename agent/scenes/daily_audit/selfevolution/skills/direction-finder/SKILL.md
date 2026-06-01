---
name: direction-finder
description: Discover one user-triggered action workflow worth auditing today and justify the choice with concrete entry evidence.
---

# Direction Finder

- Discover user-triggered workflows directly from the repository. Do not depend on a prebuilt candidate pool.
- Start from action definitions, toolbars, menus, hotkeys, context menus, launch panels, and `RunAction` / `PostAction` call sites.
- Select exactly one bounded action workflow for the current run.
- Prefer workflows with:
  - clear user reachability
  - concrete entry evidence
  - bounded investigation scope
  - meaningful correctness, performance, or optimization signal
- Return the chosen workflow with entry evidence and a concise rationale.
