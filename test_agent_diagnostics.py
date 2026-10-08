"""Regression tests for safe MCP argument handling and specialist separation."""
from __future__ import annotations
import json
import unittest
from unittest.mock import patch

import agent_diagnostics
import nawnie_server as core
from specialist_agents import agent_profile, agent_prompt


class DiagnosisTests(unittest.TestCase):
    def test_json_parser_failure_not_assumed_model_capability(self):
        result = agent_diagnostics.diagnose("Agent sends a malformed arguments_json tool call")
        self.assertEqual(result["candidates"][0]["layer"], "tool-serialization")
        self.assertFalse(result["root_cause_verified"])
        self.assertIn("Verifier", result["acceptance"])

    def test_observation_diagnosis(self):
        result = agent_diagnostics.diagnose("Screenshot is wrong and RAM game state stale")
        self.assertEqual(result["candidates"][0]["layer"], "observation-state")

    def test_unknown_failure_is_not_guessed(self):
        result = agent_diagnostics.diagnose("Something feels off")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["fallback"]["owner"], "researcher")

    def test_invalid_evidence_fails_closed(self):
        with self.assertRaises(ValueError):
            agent_diagnostics.diagnose("error", evidence=["x"] * 21)

    def test_preflight_suggests_tool_typo(self):
        result = agent_diagnostics.preflight_tool_call("shawn_core_diagnos", {}, core.TOOLS)
        self.assertFalse(result["valid"])
        self.assertIn("shawn_core_diagnose", result["suggestions"])

    def test_preflight_detects_required_and_unsupported(self):
        result = agent_diagnostics.preflight_tool_call(
            "shawn_core_diagnose", {"evidnce": ["a"]}, core.TOOLS
        )
        self.assertFalse(result["valid"])
        self.assertTrue(any("missing required argument: symptom" in err for err in result["errors"]))
        self.assertTrue(any("did you mean evidence" in err for err in result["errors"]))

    def test_preflight_rejects_wrong_json_types(self):
        result = agent_diagnostics.preflight_tool_call(
            "shawn_core_specialist_agent", {"agent": "al", "task": "inspect", "execute": "true"}, core.TOOLS
        )
        self.assertIn("execute must be boolean", result["errors"])

    def test_preflight_rejects_bool_as_integer(self):
        result = agent_diagnostics.preflight_tool_call(
            "shawn_core_specialist_execute",
            {"specialist": "al", "tool": "al_route", "timeout_seconds": True},
            core.TOOLS,
        )
        self.assertIn("timeout_seconds must be integer", result["errors"])


class SpecialistTests(unittest.TestCase):
    def call(self, name: str, args: dict) -> dict:
        return core.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})

    def test_plan_separate_wren_and_al_profiles(self):
        for agent in ("wren", "al"):
            reply = self.call("shawn_core_specialist_agent", {"agent": agent, "task": "Debug an agent integration", "execute": False})
            self.assertFalse(reply["result"]["isError"], reply)
            result = reply["result"]["structuredContent"]
            self.assertEqual(result["agent"], agent)
            self.assertEqual(result["execution"]["status"], "planned")
            self.assertEqual(result["execution"]["plan"]["sandbox"], "read-only")
        self.assertNotEqual(agent_profile("wren", "")["owns"], agent_profile("al", "")["owns"])

    def test_verifier_is_not_implementer(self):
        prompt = agent_prompt("verifier", "independent verification", "Audit repair", [])
        self.assertIn("implementing the repair", prompt)
        self.assertIn("never", prompt.casefold())

    def test_errors_provide_guidance_and_do_not_run(self):
        unknown = self.call("shawn_core_diagnos", {"symptom": "oops"})
        self.assertEqual(unknown["error"]["code"], -32602)
        self.assertIn("shawn_core_diagnose", unknown["error"]["message"])
        bad = self.call("shawn_core_specialist_agent", {"agent": "al", "task": "inspect", "execute": "true"})
        self.assertTrue(bad["result"]["isError"])
        self.assertIn("execute must be boolean", bad["result"]["content"][0]["text"])

    def test_array_arguments_not_coerced_to_empty_object(self):
        reply = core.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                             "params": {"name": "shawn_core_diagnose", "arguments": []}})
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("JSON object", reply["result"]["content"][0]["text"])

    def test_plan_never_launches_model(self):
        with patch.object(core, "tool_spawn_agent", wraps=core.tool_spawn_agent) as run:
            result = self.call("shawn_core_specialist_agent", {"agent": "al", "task": "Trace tool parser"})
            self.assertFalse(result["result"]["isError"])
            self.assertEqual(result["result"]["structuredContent"]["execution"]["status"], "planned")
            self.assertEqual(run.call_args.args[0]["execute"], False)

    def test_recommendation_uses_diagnosis(self):
        suggestions = core.command_recommendations("fix agent tool call error", include_route=False)
        self.assertEqual(suggestions[0]["tool"], "shawn_core_diagnose")


if __name__ == "__main__":
    unittest.main()
