# AES specialist swarm lifecycle

The "agent" and "MCP tool" are different entities:
- **Specialist identity:** bounded role, owned capabilities, evidence requirements and handoff contract.
- **Agent invocation:** isolated model reasoning context; shawn_core_specialist_agent executes a read-only, ephemeral Codex subprocess only when explicitly requested.
- **Tool executor:** shawn_core_specialist_execute delegates a permitted tool to a fixed local MCP specialist or native handler.
- **Orchestrator:** Nawnie assigns work, maintains the durable request ledger and redistributes unresolved work.
- **Verifier:** independently judges observable acceptance; never implement-and-self-approve.

## Contract of every specialist work packet

Use one machine-readable packet for each bounded phase:

{
  "request_id": "core-...",
  "task_id": "phase-...",
  "owner": "al",
  "goal": "Observe what the agent actually does",
  "non_goals": ["Don't retrain until parser is verified"],
  "input_artifacts": [{"path": "...", "sha256": "..."}],
  "baseline": {"expected": "...", "actual": "...", "environment": "..."},
  "hypotheses": [{"claim": "...", "falsifying_test": "..."}],
  "permissions": {"allowed_tools": [], "authorized_workspace": null},
  "acceptance_criteria": ["Test must reproduce then pass"],
  "checks": [{"command": "...", "expected": "..."}],
  "handoff_to": "verifier"
}

Don't infer field values if unavailable: mark unknown. Secrets may never go into packets or diagnostic logs.

## Workflow

1. **Intake:** Nawnie records goal/constraints and chooses implementation owner. Check active repository snapshot and AGENTS.md.
2. **Diagnosis:** shawn_core_diagnose proposes possible failure layers; record an original repro before edits.
3. **Assign:** Wren for UI/product API; AL for model/AI harness; Operator for permissioned execution; Researcher for missing sources; Agent T for security. Exactly one implementation owner per change-set.
4. **Isolated investigation:** spawn an isolated specialist if model reasoning is helpful, but do not pretend spawning alone executes a repair.
5. **Plan and permission:** enumerate files, tools, credential scope, expected side effects, rollback, acceptance. Explicit approval gates remain at host/executor.
6. **Apply:** authorized executor changes only scoped files; store diff/commit and tool receipts.
7. **Verify:** separate Verifier reruns original failing case plus at least one negative and regression case. Reject any "fixed" claim backed only by LLM narration.
8. **Integrate:** Wren checks frontend contracts if UI-facing; AL checks runtime compatibility if inference-facing. Nawnie resolves conflicts and commits canonical receipt.
9. **Resume:** from durable phase checkpoint after crashes/restarts. Replay must not repeat already-completed side effects.

## Anti-loop

After two identical repair attempts without new observation, switch the hypothesis or request more evidence. Do not increase model size, add more agents, broaden permissions, or train on weak labels as a substitute for verifying the environment.

## Split-work examples

- "Chat UI stops showing streamed images": Wren owns UI and API contract; AL owns image backend result/stream semantics; Verifier tests the full UI round-trip.
- "Pokémon agent says it healed but did not": AL audits state extraction, tool result and stale screenshots; Operator tests bounded emulator calls; Verifier checks RAM/UI ground truth after the action.
- "MCP calls an invalid tool": Core validates tool name/arguments; AL checks model template/parser; Agent T checks that the executor denies unauthorized escalation.
- "Model OOM on 16GB card": AL measures weight load, KV cache, concurrency and GPU headroom; Wren should not propose frontend fixes.

## Persistent repair lifecycle (staging implementation)

The five `shawn_core_repair_*` MCP tools persist diagnostic work under a local SQLite file. They do NOT automatically run separate agents. Distinguish a **recorded work owner** from a currently running agent and from an authenticated executor.

| Case state | Gate | Typical next owner |
| --- | --- | --- |
| `intake` | Stable `request_key`, expected/actual and owner | Nawnie |
| `investigating` | Start case; record observation, hypothesis and falsifying probe | AL / Wren / specialist |
| `planned` | `plan` requires all three evidence types | Implementation owner |
| `awaiting_verification` | `submit` requires a NEW repair artifact receipt since the latest plan | Verifier |
| `verification_reported` | Current-submission verifier receipt then `accept_report` | Nawnie + teaching record |
| `blocked` | Explicit `block`; `resume` returns to investigating | Existing owner |

`reject_report` returns to investigation. A rejected attempt does not carry a stale verifier receipt into the next submission. A new plan needs new repair artifact evidence, and a new submission needs a fresh verification reference. `handoff` changes the case's recorded owner only. It is not a permission grant.

### Tool-call example

1. `shawn_core_repair_open({"request_key":"qwen-chat-parser-20261008","project":"Qwen Chat","symptom":"tool result disappears","expected":"parsed result reaches UI","actual":"empty result","owner":"al"})`
2. Save the returned `case_id` and `revision`. Invoke `shawn_core_repair_transition(... action="start", expected_revision=0, actor="al", event_id="qwen-start-1", reason="collect raw response")`.
3. Append observation, hypothesis, probe (each with a distinct `event_id` and updated `expected_revision`); move through `plan`, repair receipt, and `submit`.
4. Verifier records an independent `verification` note with `source_ref` to an actual test result. Only then use `accept_report` or `reject_report`. Treat the state as a *reported result*, not cryptographic or deployment certification.
5. Append `lesson` after acceptance. Provide the user the measured outcome, a technical explanation, and any remaining uncertainty.
6. On interruption call `shawn_core_repair_list` then `shawn_core_repair_get`; use the persisted revision to continue without replaying completed side effects.

The ledger uses optimistic concurrency and stable event IDs; a repeated request with the same ID and payload is idempotent. The host still must authorize tool calls, authenticate identity, and redact evidence. Run `python -m unittest -v test_agent_diagnostics test_repair_ledger test_repair_gateway` in the staged checkout; isolated tests alone do not verify installed child MCPs, Codex subprocesses, GPU runtime or Windows configuration.
