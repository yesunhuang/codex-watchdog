---
name: codex-use-claude
description: Delegate implementation-heavy coding work from Codex to Claude CLI when Claude is available and its use is authorized. Use for code generation, repetitive refactors, test scaffolding, glue code, data converters, plotting/report tooling, or bounded bug fixes with a clear contract; keep architecture, scientific or product judgment, provenance, acceptance, integration decisions, and sensitive actions with Codex.
---

# Codex Use Claude

Use Claude CLI as implementation labor, not as the final decision-maker.

The default division of labor is:

- **Claude writes code.**
- **Codex scopes the task, defines the contract, validates the result, and makes judgment calls.**

The purpose of delegation is to save Codex time and attention. Do not erase that benefit by independently redoing Claude's work after it finishes.

## 1. Check availability before delegating

Before the first Claude task in a workspace:

1. Check whether the Claude CLI exists and report its version.
2. Check whether it is already authenticated with a harmless, non-mutating invocation.
3. If the CLI is missing and installation is authorized, use the provider's current official user-space installation path.
4. If interactive login is required, stop at that point and ask the user to complete login.
5. Respect the environment's configured model ceiling, quota, budget, and usage policy. Do not request a model or paid capacity that is outside the already-authorized scope.

Do not provision new paid compute merely to gain access to Claude unless separately authorized.

## 2. Delegate the right work

Prefer Claude for implementation-heavy or repetitive work whose desired behavior can be specified clearly, including:

- new modules with a settled interface;
- runner, controller, harvester, parser, or report-generation code;
- repetitive refactors and mechanical migrations;
- glue code and adapters;
- test scaffolding and focused unit tests;
- data/result conversion utilities;
- plotting or table-generation code;
- bounded bug fixes with a known failing behavior;
- boilerplate, serialization, configuration, and CLI plumbing.

Keep Codex responsible for work dominated by judgment, including:

- architecture and algorithm selection;
- experiment design and acceptance criteria;
- scientific interpretation;
- product or research conclusions;
- provenance and integrity adjudication;
- permission, security, privacy, cost, and resource decisions;
- merge/conflict strategy and final integration decisions;
- deciding whether a surprising result is real, expected, or a bug.

Claude may assist with these areas only as a source of suggestions. Codex remains the owner of the decision.

## 3. Give Claude a bounded implementation contract

Before invoking Claude, reduce the task to a concrete contract. Include only the context needed to implement it.

Specify:

- **Goal:** observable behavior to add or fix.
- **Allowed scope:** files or modules Claude may change.
- **Interfaces:** functions, classes, schemas, CLI flags, or file formats that must be preserved.
- **Invariants:** behavior that must not change.
- **Tests:** focused checks that should pass.
- **Non-goals:** tempting adjacent work that is out of scope.
- **Side-effect limits:** whether the task is code-only, may run tests, may write files, etc.
- **Completion condition:** what counts as done.

Prefer a small number of cohesive coding units over one giant open-ended request.

A useful prompt skeleton is:

```text
Implement this bounded task in the current repository.

Goal:
...

Allowed files / modules:
...

Required interfaces and invariants:
...

Tests or observable checks:
...

Do not:
- broaden the task;
- change unrelated behavior;
- perform external mutations outside the authorized scope;
- make architecture/scientific decisions on my behalf.

When finished, summarize changed files, tests run, and any uncertainty.
```

Ask Claude to implement rather than merely discuss when implementation is the goal.

## 4. Minimize sensitive context

Send Claude the minimum context necessary for the coding task.

Do not include:

- credentials, API keys, tokens, cookies, or passwords;
- personal identifiers or personal history;
- private messages or unrelated conversation logs;
- institution, account, host, network, or financial details that are not required for implementation;
- unrelated proprietary or confidential material.

Prefer repository paths, abstract role names, generic machine labels, and interface descriptions over personal or organizational context.

If a coding task can be completed from repository files alone, do not add external personal context.

Follow the repository's existing confidentiality and data-handling rules. This skill does not grant new permission to share data with external services.

## 5. Validate; do not duplicate

After Claude finishes, Codex should normally validate the work with:

1. inspect the diff and changed-file list;
2. run focused tests;
3. check the requested interfaces and invariants;
4. run a small smoke test when appropriate;
5. spot-check the highest-risk logic.

**Do not independently reimplement the same task just to verify Claude.**

**Do not line-by-line re-audit everything by default.**

Escalate to deeper review or Codex reimplementation only when there is a concrete red flag, such as:

- failing tests;
- interface drift;
- suspicious or unexplained code;
- provenance/integrity mismatch;
- unsafe side effects;
- security or privacy concerns;
- numerically implausible output;
- repeated local defects suggesting Claude misunderstood the contract.

For a localized defect, prefer a targeted correction request to Claude or a small Codex patch over rewriting the whole implementation.

## 6. Preserve repository workflow

Follow the repository's existing AGENTS.md, branch, commit, provenance, and approval rules.

By default:

- Claude may implement within the already-authorized local coding scope.
- Codex owns acceptance and publication of Claude's work.
- Do not let Claude merge, resolve protected conflicts, push protected refs, spend money, provision resources, or make other externally consequential decisions unless that action is independently authorized.
- Prefer additive, modular code that integrates cleanly with the current mainline.
- Do not create unnecessary temporary branches merely because Claude was used.

Claude is a coding worker inside the workflow, not a replacement for the workflow.

## 7. Measure whether delegation is actually useful

When evaluating Claude usage, track enough evidence to improve future routing.

For each meaningful delegated coding unit, classify the accepted result as one of:

- **unchanged:** accepted after focused validation with no code correction;
- **minor edit:** small localized correction, cleanup, or interface adjustment;
- **material rework:** substantial logic or structure had to be corrected before use;
- **discarded:** implementation was not usable.

Also record, when practical:

- first-pass focused-test success;
- number of nonfatal bugs found during validation;
- number of potentially fatal defects;
- approximate Codex rework time;
- approximate time/attention saved;
- Claude model used, when relevant to routing future work.

Treat a defect as **potentially fatal** when accepting it could invalidate results, corrupt data or state, violate an invariant, create a serious security/privacy problem, or cause materially incorrect external behavior.

A high useful-delegation rate with low material rework is evidence to offload more tasks of that class. Repeated material or fatal defects are evidence to keep that class with Codex or tighten the contract.

Do not create redundant audit work solely to collect these metrics.

## 8. Route future work from observed quality

Use actual experience rather than model reputation alone.

Examples:

- If a model repeatedly produces reliable settled-interface modules, route similar heavy implementation work to it.
- If it struggles with provenance-sensitive orchestration, give it narrower helpers while Codex keeps the orchestration.
- If a cheaper/faster model is consistently adequate for boilerplate, prefer it there.
- Reserve stronger models for implementation units whose complexity justifies them.

Re-evaluate routing when the task type changes.

## 9. Stop conditions

Do not delegate when:

- the task is trivial enough that delegation overhead exceeds the work;
- Claude is unavailable, unauthenticated, or out of authorized quota;
- the task requires context that should not be shared;
- the dominant difficulty is judgment rather than implementation;
- the required side effects exceed current authorization.

If Claude cannot be used, continue with Codex when the task is otherwise within scope, or report the blocking condition when it is not.
