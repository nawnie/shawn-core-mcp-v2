# Shawn Core v2 specialist-agent diagnostics (staging)

This is additive to the EXISTING Shawn Core MCP. It is NOT installed on Windows until the active plugin checkout is reconciled.

## Tools

- shawn_core_diagnose: classify candidate failure layers, give falsifiable probes and owner; not a verified root cause.
- shawn_core_tool_help: validate Core tool names and JSON arguments, no execution.
- shawn_core_specialist_agent: plan/execute an independent role-specific read-only Codex invocation. Default execute=false. Separate context is not permanent memory.
- Root tools/call: rejects wrong argument types or missing required fields and suggests similar tool names.
- shawn_core_specialist_tools / shawn_core_specialist_execute: retain the old specialist tool execution path. Tool execution and separate model-agent execution are different operations.

## JSON-RPC examples

Diagnose:
    {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"shawn_core_diagnose","arguments":{"symptom":"Agent says it healed but RAM shows HP unchanged","evidence":["No YES-prompt receipt"]}}}

Check syntax:
    {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"shawn_core_tool_help","arguments":{"tool_name":"al_route","arguments_json":"{\"taks\":\"Investigate wrong tool parser\"}"}}}

Plan isolated specialist:
    {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"shawn_core_specialist_agent","arguments":{"agent":"al","task":"Trace raw model output through tool parsing","execute":false}}}

execute=true starts the existing bounded read-only Codex subprocess if that runtime is available. It does NOT execute repair edits, deploy updates or create a durable always-on service.

## Tests

Run from a checkout with these files side by side:

    python -m py_compile nawnie_server.py agent_diagnostics.py specialist_agents.py test_agent_diagnostics.py
    python -m unittest -v test_agent_diagnostics

Existing full gateway tests assume the plugin-pack mcp/ layout and installed child specialists. Test after deployment too; isolated tests are not a substitute for integration verification.

Review the active workstation checkout (which may be ahead of this repository's 0.8.1 snapshot), reconcile changes, validate the installed gateway and record rollback before deploying. Protocol upgrade to MCP 2026-07-28 is a separate migration, not a version-string change.
