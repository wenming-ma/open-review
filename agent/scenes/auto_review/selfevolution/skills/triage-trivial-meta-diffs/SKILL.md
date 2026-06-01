---
name: triage-trivial-meta-diffs
description: "Review tiny or meta-only diffs such as logs, trigger markers, doc-only touches, and repository metadata changes without over-escalating. Use when the changed scope is small, non-product-facing, or primarily verification noise."
---

# Triage Trivial Or Meta-Only Diffs

Use this skill when the MR scope is dominated by tiny, non-product changes such as:

- log files
- trigger markers
- doc-only updates
- repository housekeeping artifacts
- one-line test-touch files

The goal is not to ignore these changes. The goal is to review them proportionally.

## Step 1: Lock The Scope

Before reasoning outward:

1. Read the frozen MR scope snapshot.
2. Confirm exactly which files changed and whether they are code, config, docs, logs, or metadata.
3. Stay inside that scope unless the diff gives concrete evidence that another workflow is affected.

Do not widen the review just because a filename hints at a broader system.

## Step 2: Separate Observations From Problems

Keep three buckets separate:

1. **Actionable negative issue**
   - Something a reviewer should change, revert, or follow up on.
   - This can become a finding.
2. **Positive or neutral observation**
   - Examples: no sensitive data present, scope is narrow, no runtime code changed.
   - Keep these in notes or evidence, not findings.
3. **Uncertain concern**
   - Something that might matter, but the current diff does not prove it.
   - Keep this as an open question, not a confirmed issue.

## Step 3: Apply A Proportional Review Standard

For trivial or meta-only diffs:

- default to no finding unless there is a concrete reviewer-actionable problem
- do not turn one narrow concern into multiple overlapping findings
- avoid broad architectural theories that the diff does not support
- keep the summary calm and bounded

Valid examples of real findings in this class of diff:

- a runtime log file was committed because `.gitignore` is missing an ignore rule
- a marker file leaks credentials, secrets, internal paths, or privileged infrastructure details
- a test-trigger artifact changes CI or release behavior in a dangerous way

Non-findings that belong in notes instead:

- no sensitive data was found
- the diff appears safe
- only timestamps changed
- no product code was modified

## Step 4: Keep Recommendation Aligned With Impact

If the only real issue is a low-risk hygiene concern:

- report it proportionally
- do not inflate the language
- prefer one concise issue over several restatements

If there is no clear reviewer-actionable problem:

- keep findings empty
- summarize why the diff is low risk
- preserve any uncertainty as an open question only when it truly remains unresolved
