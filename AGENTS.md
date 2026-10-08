# Shawn Core MCP — agent rules (2026-10-08)

This is the **existing Shawn Core v2** gateway, not a new gateway or an instruction to replace the running Hermes / AES / MoK stack. The checked-in code is a snapshot; inspect the workstation's active checkout and installed plugin version before deploying. Root AES and aeswiki AGENTS.md remain governing where applicable.

## Authority and evidence

1. Nawnie owns decomposition, routing, ownership conflicts, work ledger and integration.
2. AL owns model artifacts, tokenizers/chat templates, parsers, GPU, inference, AI-serving/training and AI harness state extraction. Do not route general backend work to AL solely because it uses Python/FastAPI.
3. Wren owns UI/UX, browser integration, frontend contracts and product-facing API behavior. AI inference internals belong to AL.
4. Operator executes narrowly permitted shell/file operations under the domain owner's plan; it does not own design.
5. Researcher collects external evidence and counterexamples. Agent T owns security testing.
6. Verifier independently accepts/rejects the change. The implementer cannot self-certify it.
7. MoK is a model/router/capacity service, not an owner of every agent's objectives, memory or filesystem.

## Before touching a broken agent

**Do not assume the model is the problem.** Call shawn_core_diagnose with the exact symptom and any traces. Classify candidates into: model/runtime/serializer, tool execution, environment observation, memory/task state, resources, training/evaluation, authorization, frontend integration.

Required diagnostic receipt:
- Original user goal, expected behavior and exact observed deviation.
- Reproducible request + environment/model/runtime version + model artifact ID/hash where relevant.
- Raw tool-call output, advertised tool schema, authorization result and final tool response when relevant.
- Observations with timestamps and ground truth sources; do not replace evidence with narration.
- Separate verified facts from competing hypotheses and untested assumptions.
- A discriminating test that would falsify the leading hypothesis.
- A minimal bounded repair, reversible plan and regression tests.
- Independent Verifier result, actual checks and hashes/commits where available.

**Typical failure routing:** malformed tool calls -> AL/parser/schema; tool errors -> Operator plus owning engineer; stale screen/game state -> AL/harness; context loss -> Nawnie/ledger; model OOM -> AL/runtime; frontend mismatch -> Wren; prompt injection -> Agent T; training failure -> AL/dataset/eval.

## MCP usage

- shawn_core_tool_help checks core tool names, required fields and common field typing **without running the target**. Do not automatically coerce or invent arguments.
- shawn_core_specialist_tools lists a specialist's actual registered tool surface. shawn_core_specialist_execute calls an owned MCP tool, **not** a separate LLM agent.
- shawn_core_specialist_agent plans an independent read-only Codex run when execute=false. execute=true invokes one bounded, ephemeral, role-specific Codex subprocess if that runtime exists.
- A subprocess is not a persistent autonomous service; it has no guaranteed independent memory, custom credential scope, or write permission. Durable state/receipts remain Core's responsibility.
- Read-only model analysis is not approval for applying changes. Mutations require an independent authorized executor and any necessary user approval.
- Preserve 2025-11-25 protocol compatibility while the existing host/tests use that version. Migrating to 2026-07-28 requires a separate tested protocol/SDK upgrade; do not change version strings alone.

## Branch / review contract

Treat this work as a reviewable **staging branch**. Never overwrite a user's active checkout, local model configuration, agent memory or current deployment without inspecting its exact version/diff.

Changes are done only when source compiles, isolation/tests pass, Verifier signs off and deployed host behavior is observed. A pull request or a model's assertion is not deployment verification. Follow swarm.md for specialist lifecycle and handoffs.

## Durable repair cases and learning contract (staged)

Use the local SQLite-backed repair tools as the continuity ledger for significant debugging work:
- `shawn_core_repair_open`: call once per incident with a stable caller-supplied `request_key`, project, symptom, expected and actual behavior, and a single implementation owner.
- `shawn_core_repair_transition` with `action=start` begins investigation. Append `observation`, `hypothesis` and `probe` notes before `plan`; describe a test that could falsify the leading explanation.
- Record `repair` with a commit/diff/change receipt before `submit`. This is a receipt reference, not permission to execute the repair.
- A separate Verifier records `verification` with a current test-result reference before `accept_report` or `reject_report`. The receipt must belong to the current submission. The resulting `verification_reported` state is **a stored claim**; the host must authenticate the actor and independently validate actual results.
- After a reported acceptance, append a `lesson` explaining the failure layer, causal mechanism, how to recognize it, and why the bounded repair worked. `shawn_core_repair_get` exposes missing learning elements and all event history; `shawn_core_repair_list` supports continuation after restart.
- On a handoff, use an explicit `handoff` transition to change the recorded owner, then pass the exact `case_id`, latest `revision`, and evidence. A handoff **never** changes tool access or authorizes a mutation.

All state mutations require stable `event_id` and current `expected_revision`. Duplicate IDs with identical content are safely reusable; conflicting IDs and stale revisions are rejected. Never put passwords, tokens, customer data, full private logs or sensitive screenshots in this ledger. Token-pattern filtering is best-effort only; callers must redact at source. `SHAWN_CORE_REPAIR_LEDGER` may be set by the trusted host to specify its file path; the MCP client cannot choose a write path.

This is local single-operator continuity, **not** a multi-tenant authorization system, scheduler, autonomous worker process or verified production deployment. The `actor` field in these tools is caller supplied: host-side identity and permission checks remain a deployment prerequisite.

## Every completed repair should teach

Give the user a concise, technically precise repair explanation:
1. **Observed failure:** expected versus actual, exact runtime/tool/environment where established.
2. **Root cause:** what was proven and by which discriminating observation; label conjecture clearly.
3. **Why the fix works:** precise mechanism and layer ownership, not just changed files.
4. **Regression:** named checks, actual receipts, untested areas and rollback.
5. **Transferable lesson:** one reusable debugging principle and the next test the user could run unaided.

Do not upgrade the model, retrain, add agents, or grant broader permissions as a substitute for isolating a parser, sensor, tool-execution, state or UI failure.
