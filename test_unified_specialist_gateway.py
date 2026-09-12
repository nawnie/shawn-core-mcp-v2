#!/usr/bin/env python3
"""End-to-end acceptance: every specialist is executable through one Core MCP."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
import nawnie_server
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "mcp" / "nawnie_server.py"

CASES = {
    "nawnie": ("shawn_core_context", {}),
    "ted": ("ted_enter_shed", {}),
    "victoria": ("victoria_enter_desk", {}),
    "agent-t": ("agent_t_status", {}),
    "corporate-overview": ("corporate_overview_enter_desk", {}),
    "sensai-advisor": ("sensai_advisor_enter_desk", {}),
    "verifier": ("verifier_enter_desk", {}),
    "researcher": ("researcher_enter_desk", {}),
    "legal-readiness": ("legal_readiness_enter_desk", {}),
    "wren": ("wren_enter_desk", {}),
    "cad": ("cad.capabilities", {}),
    "al": ("nawnie_manifest_status", {"scope": "model"}),
    "operator": ("operator_capabilities", {}),
    "creation-kit": ("creation_kit_enter", {}),
    "mechanical-engineer": ("mechanical_wren_session", {"session_id": "unified-core-e2e-acceptance", "speaker": "mechanical-engineer", "message": "Acceptance-only thermal requirement.", "constraints": ["test receipt only"]}),
    "token-master": ("token_master_audit", {}),
    "chrono": ("chrono_plan", {"task": "Plan an acceptance-only implementation with contingencies."}),
    "changelog": ("changelog_intake", {"task": "Unified Core e2e acceptance", "execute": False}),
}


class UnifiedSpecialistGatewayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-B", str(SERVER)], cwd=ROOT, text=True, encoding="utf-8",
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            **nawnie_server.hidden_subprocess_kwargs(),
        )
        assert self.process.stdin and self.process.stdout
        self.request_id = 0
        self.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "unified-core-e2e", "version": "1"}})
        self.notify("notifications/initialized", {})

    def tearDown(self) -> None:
        assert self.process.stdin
        self.process.stdin.close()
        self.process.wait(timeout=15)
        stderr = self.process.stderr.read() if self.process.stderr else ""
        if self.process.stdout:
            self.process.stdout.close()
        if self.process.stderr:
            self.process.stderr.close()
        self.assertEqual(self.process.returncode, 0, stderr)

    def notify(self, method: str, params: dict) -> None:
        assert self.process.stdin
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n")
        self.process.stdin.flush()

    def rpc(self, method: str, params: dict) -> dict:
        self.request_id += 1
        assert self.process.stdin and self.process.stdout
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        return json.loads(self.process.stdout.readline())

    def call(self, name: str, arguments: dict) -> dict:
        reply = self.rpc("tools/call", {"name": name, "arguments": arguments})
        self.assertNotIn("error", reply, reply)
        result = reply["result"]
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def test_all_eighteen_specialists_list_and_execute(self) -> None:
        verified = []
        for specialist, (tool, arguments) in CASES.items():
            listed = self.call("shawn_core_specialist_tools", {"specialist": specialist})
            self.assertGreater(listed["tool_count"], 0, specialist)
            names = {item["name"] for item in listed["tools"]}
            self.assertIn(tool, names, specialist)
            executed = self.call("shawn_core_specialist_execute", {"specialist": specialist, "tool": tool, "arguments_json": json.dumps(arguments)})
            self.assertEqual(executed["specialist"], specialist)
            self.assertEqual(executed["tool"], tool)
            verified.append(specialist)
        self.assertEqual(verified, list(CASES))


if __name__ == "__main__":
    unittest.main(verbosity=2)
