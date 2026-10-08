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
