---
name: code-simplifier
description: Keep target migration code clear, consistent and maintainable while preserving behavior and frozen assertions.
---

# ModPort Code Simplifier

This is a ModPort-modified adaptation, distributed under Apache-2.0;
see the accompanying LICENSE and NOTICE for its upstream source and changes.

Adapted from the universal readability principles in the installed code-simplifier
skill, based on Anthropic's code-simplifier agent:
https://github.com/anthropics/claude-plugins-official/blob/main/plugins/code-simplifier/agents/code-simplifier.md

Apply these principles during implementation and repair as well as cleanup. Stay
within the assigned work package and recently changed code. The host's rules,
locked versions, credential-free execution permissions and frozen contracts govern
the assignment. This skill is guidance, not a separate approval or acceptance gate.

## Preserve behavior

Change how the code expresses the assigned behavior without changing the behavior
itself. Preserve public interfaces, mod identity, resources, registrations,
configuration and save formats, side separation, networking and observable outputs.
Keep frozen requirement IDs, test IDs, assertion logic and expected outcomes intact.
Never delete, skip, rename, weaken or replace an assertion to make cleanup pass.
Read source-derived requirements; do not generate, run or repair source harnesses.

## Follow the actual project

Read the project's conventions and nearby code before choosing a structure. Use
the exact locked Java, Minecraft and NeoForge versions and their official APIs;
do not assume a language feature or API from a later version is available. Keep
the established Java indentation, import grouping, type annotations and naming
patterns. Prefer clear type and method names, explicit imports and straightforward
branches. Preserve existing exception handling contracts and useful diagnostics.

Keep client-only classes out of server initialization. Preserve event ordering,
registration lifecycle, thread ownership, packet encoding and persistence
compatibility. Before removing apparently unused code, inspect registration,
reflection, mixins, generated data, service loading and resource references.
In build scripts and harness helpers, follow their existing language conventions
and the official target project layout rather than forcing Java rules onto them.

## Make intent easier to read

Reduce unnecessary nesting and duplication. Give variables and methods names that
explain their purpose. Keep methods focused on one responsibility, and consolidate
related logic when its behavior and ownership remain clear. Prefer explicit
if/else or switch branches to nested ternaries. Avoid dense one-liners and clever
shortcuts that make control flow, side effects or failure handling harder to see.

Remove redundant wrappers and abstractions when direct code is easier to follow.
Keep abstractions that express domain meaning, isolate side effects or clarify
ownership. Keep comments that explain intent or version constraints; remove
comments that merely repeat the adjacent statement. Do not prioritize fewer lines
over debuggability or combine unrelated responsibilities for compactness.

## Work within the host workflow

Review the changed sections for equivalent behavior and readable intent. Use only
the validation actions allowed by the assignment; the host owns sandbox execution
and subsequent verification. Report actual changes and unresolved concerns
concisely. No edits can be a valid cleanup result. Do not stage or commit unless
the assignment permits it. Do not introduce approvals, new identity comparisons,
hashes, checksums or fingerprint gates. Preserve original evidence and frozen inputs.
