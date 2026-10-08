#!/usr/bin/env python3
"""Black-box JSON-RPC smoke tests for the local Nawnie MCP server."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import nawnie_server


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "mcp" / "nawnie_server.py"


class NawnieMcpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.process = subprocess.Popen(
            [sys.executable, str(SERVER)], cwd=ROOT, text=True,
            encoding="utf-8", stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            **nawnie_server.hidden_subprocess_kwargs(),
        )
        assert self.process.stdin and self.process.stdout

    def tearDown(self) -> None:
        if self.process.stdin:
            self.process.stdin.close()
        self.process.wait(timeout=10)
        stderr = self.process.stderr.read() if self.process.stderr else ""
        if self.process.stdout:
            self.process.stdout.close()
        if self.process.stderr:
            self.process.stderr.close()
        self.assertEqual(self.process.returncode, 0, stderr)

    def rpc(self, request: dict) -> dict:
        assert self.process.stdin and self.process.stdout
        self.process.stdin.write(json.dumps(request) + "\n")
        self.process.stdin.flush()
        return json.loads(self.process.stdout.readline())

    def call(self, name: str, arguments: dict) -> dict:
        reply = self.rpc({"jsonrpc": "2.0", "id": name, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
        self.assertFalse(reply["result"]["isError"], reply)
        return reply["result"]["structuredContent"]

    def test_protocol_and_tools(self) -> None:
        init = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "shawn-core")
        listing = self.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        self.assertEqual({tool["name"] for tool in listing["result"]["tools"]}, {"shawn_core_context", "shawn_core_query", "shawn_core_diagnose", "shawn_core_tool_help", "shawn_core_specialist_agent", "shawn_core_port_validate", "shawn_core_validate", "shawn_core_tool_inventory", "shawn_core_specialist_tools", "shawn_core_specialist_execute", "nawnie_status", "token_master_audit", "chrono_plan", "changelog_intake", "changelog_finalize", "mechanical_wren_session", "game_dev_capabilities", "game_dev_route", "nawnie_recommend_commands", "nawnie_manifest_status", "al_capabilities", "al_route", "operator_capabilities", "operator_file", "operator_terminal", "carl_enter_workshop", "carl_route", "carl_plugin_audit", "carl_creation_kit_audit", "carl_package_audit", "nawnie_route", "nawnie_call_agent", "nawnie_spawn_agent", "nawnie_compare"})
        port = self.call("shawn_core_port_validate", {"service_name": "test server", "preferred_port": 4182, "range_start": 4182, "range_end": 4184, "bind": "loopback"})
        self.assertNotIn(port["assigned_port"], {4182})
        self.assertFalse(port["firewall_change_authorized"])

    def test_specialist_tool_inventory_rechecks_registered_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "sfse_loader.exe"
            executable.write_bytes(b"test")
            registry = root / "registry.json"
            registry.write_text(json.dumps({"machine": "test", "captured_utc": "2026-08-28T00:00:00Z", "specialists": {"creation-kit": {"tools": [{"id": "sfse-loader", "path": str(executable), "exists": True}]}}}), encoding="utf-8")
            with patch.object(nawnie_server, "SPECIALIST_TOOL_REGISTRY", registry):
                result = nawnie_server.tool_core_tool_inventory({"specialist_id": "carl"})
            self.assertEqual(result["summary"], {"specialists": 1, "present_paths": 1, "missing_paths": 0})
            self.assertTrue(result["specialists"]["creation-kit"]["tools"][0]["registry_presence_matches"])

    def test_ted_route_prefers_its_declared_installed_version(self) -> None:
        ted_root, ted_entry = nawnie_server.CHILD_MCP_ROUTES["ted"]
        self.assertEqual(ted_root.name, "0.2.0+codex.20260828225924")
        self.assertTrue((ted_root / ted_entry[0]).is_file())

    def test_status_routing_handoff_and_plan(self) -> None:
        status = self.call("nawnie_status", {})
        self.assertTrue(status["router_present"])
        self.assertGreater(status["skill_count"], 0)
        self.assertFalse(any(item["name"] == "nawnie@personal" for item in status["codex_plugins"]["enabled"]))
        self.assertGreater(status["installed_skill_catalog"]["unique_skills"], 0)
        commands = self.call("nawnie_recommend_commands", {"task": "Choose a local model and research dataset for a loader."})
        self.assertEqual([command["tool"] for command in commands["recommended_commands"][:2]], ["nawnie_manifest_status", "nawnie_manifest_status"])
        manifests = self.call("nawnie_manifest_status", {"scope": "model"})
        self.assertIn("present", manifests["model_inventory"])
        if manifests["model_inventory"]["present"]:
            self.assertGreater(manifests["model_inventory"]["asset_count"], 0)
        routed = self.call("nawnie_route", {"task": "Build a marketing SEO campaign with a finance budget review."})
        self.assertTrue(routed["route"]["selected"])
        self.assertEqual({item["specialist"] for item in routed["specialist_handoffs"]}, {"ted", "victoria"})
        handoff = self.call("nawnie_call_agent", {"agent": "Ted", "task": "Review this funding budget."})
        self.assertEqual(handoff["handoff"]["mcp_server"], "shawn-core")
        self.assertEqual(handoff["handoff"]["first_tool"], "shawn_core_specialist_execute")
        planned = self.call("nawnie_spawn_agent", {"prompt": "Read this repository and summarize its status.", "model": "gpt-5.6-sol", "reasoning_effort": "high"})
        self.assertEqual(planned["status"], "planned")
        self.assertEqual(planned["plan"]["sandbox"], "read-only")
        self.assertIn("--output-last-message", planned["plan"]["command"])
        self.assertNotIn("--json", planned["plan"]["command"])

    def test_core_query_context_and_validation_loop(self) -> None:
        context = self.call("shawn_core_context", {})
        self.assertEqual(context["orchestrator"], "nawnie")
        self.assertEqual(context["general_capability_pack"], "AES")
        self.assertEqual(context["specialists"]["victoria"]["mcp_server"], "shawn-core")
        self.assertEqual(context["specialists"]["creation-kit"]["first_tool"], "shawn_core_specialist_execute")
        self.assertEqual(context["specialists"]["creation-kit"]["specialist_tool"], "creation_kit_enter")
        self.assertEqual(context["specialists"]["creation-kit"]["mcp_server"], "shawn-core")
        self.assertIn("canonical Creation Kit MCP tools", context["specialists"]["creation-kit"]["tool_contract"])
        self.assertIn("spreadsheets", context["specialists"]["ted"]["host_capabilities"])
        self.assertEqual(context["specialists"]["token-master"]["first_tool"], "shawn_core_specialist_execute")
        self.assertEqual(context["specialists"]["chrono"]["specialist_tool"], "chrono_plan")
        self.assertEqual(context["specialists"]["changelog"]["first_tool"], "shawn_core_specialist_execute")
        self.assertEqual(context["specialists"]["operator"]["specialist_tool"], "operator_capabilities")
        self.assertEqual(context["specialists"]["al"]["specialist_tool"], "al_capabilities")
        self.assertIn("shawn_core_port_validate", context["server_creation_policy"])
        self.assertIn("CUDA", context["specialists"]["al"]["role"])
        self.assertIn("product-facing API", context["specialists"]["wren"]["role"])
        self.assertIn("does not design large", context["specialists"]["operator"]["tool_contract"])

        token_audit = self.call("token_master_audit", {})
        self.assertEqual(token_audit["specialist"], "token-master")
        self.assertTrue(token_audit["active_facts"]["shawn_core_always_enabled"])
        self.assertIn("recommendations only", token_audit["change_authority"])
        changelog = self.call("changelog_intake", {"task": "Capture this change"})
        self.assertEqual(changelog["subprocess_policy"]["model"], "gpt-5.6-luna")
        self.assertEqual(changelog["subprocess_policy"]["compact_at_tokens"], 64000)
        self.assertEqual(changelog["subprocess"]["status"], "planned")
        final = self.call("changelog_finalize", {"title": "Capture this change", "changes": ["Added the end receipt path"], "receipts": ["python -m unittest"], "blockers": []})
        self.assertEqual(final["phase"], "end")
        session = self.call("mechanical_wren_session", {"session_id": "test-device", "speaker": "mechanical-engineer", "message": "Need the thermal envelope.", "constraints": ["portable"]})
        self.assertEqual(session["specialists"], ["mechanical-engineer", "wren"])

        request = self.call("shawn_core_query", {
            "task": "@victoria build a marketing campaign and preserve her web app tools",
            "constraints": ["draft only"],
            "acceptance_criteria": ["Victoria owns the route"],
        })
        self.assertEqual(request["specialist_plan"][0]["specialist"], "victoria")
        self.assertEqual(request["specialist_plan"][0]["first_tool"], "shawn_core_specialist_execute")
        self.assertFalse(request["aes_fallback"]["use"])
        self.assertEqual(request["server_creation_gate"]["tool"], "shawn_core_port_validate")

        rejected = self.call("shawn_core_validate", {
            "request_id": request["request_id"], "owner": "victoria", "status": "partial",
            "acceptance_met": False, "deterministic_checks_passed": True,
            "evidence": ["draft exists"], "receipts": [], "blockers": ["campaign validation missing"],
            "recommended_next_specialist": "nawnie",
        })
        self.assertEqual(rejected["outcome"], "redistribute")
        self.assertTrue(rejected["nawnie_intervention_required"])

        accepted = self.call("shawn_core_validate", {
            "request_id": request["request_id"], "owner": "victoria", "status": "completed",
            "acceptance_met": True, "deterministic_checks_passed": True,
            "evidence": ["validated campaign"], "receipts": ["exit 0"], "blockers": [],
        })
        self.assertEqual(accepted["outcome"], "accepted")
        self.assertFalse(accepted["nawnie_intervention_required"])

    def test_chrono_owns_core_plan_mode_with_bounded_contingencies(self) -> None:
        direct = self.call("chrono_plan", {
            "task": "Implement a React frontend with product-facing API integration.",
            "constraints": ["preserve existing routes"],
            "acceptance_criteria": ["browser smoke passes"],
        })
        self.assertEqual(direct["specialist"], "chrono")
        self.assertEqual(direct["reasoning_round"]["participants"], ["nawnie", "token-master", "chrono"])
        self.assertEqual(direct["reasoning_round"]["round_count"], 1)
        self.assertFalse(direct["reasoning_round"]["side_effects"])
        self.assertEqual({item["branch"] for item in direct["contingency_model"]["branches"]}, {"ready", "input-gap", "capability-gap", "validation-failure"})
        self.assertTrue(direct["efficiency_policy"]["token_master_receipt_required"])

        planned = self.call("shawn_core_query", {
            "task": "Implement a React frontend with product-facing API integration.",
            "mode": "plan",
            "constraints": ["preserve existing routes"],
            "acceptance_criteria": ["browser smoke passes"],
        })
        self.assertEqual(planned["mode"], "plan")
        self.assertEqual(planned["specialist_plan"][0]["specialist"], "chrono")
        self.assertEqual(planned["chrono_plan"]["specialist"], "chrono")
        self.assertIn("Chrono: plan owner", planned["routing_visualization"]["mermaid"])

        legacy = self.call("shawn_core_query", {"task": "/plan map the existing repository before edits"})
        self.assertEqual(legacy["mode"], "plan")

    def test_coding_ownership_spread_and_al_callable_surface(self) -> None:
        ai = self.call("shawn_core_query", {"task": "Implement CUDA kernels and an AI FastAPI inference service"})
        self.assertEqual([item["specialist"] for item in ai["specialist_plan"]], ["al", "verifier"])
        self.assertFalse(ai["aes_fallback"]["use"])
        self.assertIn("Runtime and compatibility proof", ai["routing_visualization"]["mermaid"])

        web = self.call("shawn_core_query", {"task": "Build a React frontend with product-facing API integration"})
        self.assertEqual([item["specialist"] for item in web["specialist_plan"]], ["wren", "verifier"])
        self.assertNotIn("al", {item["specialist"] for item in web["specialist_plan"]})

        terminal = self.call("shawn_core_query", {"task": "Write a small BAT script and run a bounded PowerShell command"})
        self.assertEqual([item["specialist"] for item in terminal["specialist_plan"]], ["operator", "verifier"])
        self.assertIn("User permission and path gate", terminal["routing_visualization"]["mermaid"])

        generic = self.call("shawn_core_query", {"task": "Implement a general Python FastAPI CRUD backend"})
        self.assertEqual([item["specialist"] for item in generic["specialist_plan"]], ["verifier"])
        self.assertTrue(generic["aes_fallback"]["use"])

        al_tools = self.call("shawn_core_specialist_tools", {"specialist": "al"})
        self.assertEqual({item["name"] for item in al_tools["tools"]}, {"al_capabilities", "al_route", "nawnie_manifest_status", "nawnie_recommend_commands"})
        capabilities = self.call("shawn_core_specialist_execute", {"specialist": "al", "tool": "al_capabilities", "arguments_json": "{}"})
        self.assertTrue(any("general application backends" in item for item in capabilities["result"]["does_not_own"]))
        routed = self.call("shawn_core_specialist_execute", {"specialist": "al", "tool": "al_route", "arguments_json": json.dumps({"task": "Build a CUDA-backed FastAPI model endpoint with frontend integration"})})
        self.assertIn("ai-fastapi-service", routed["result"]["lanes"])
        self.assertTrue(routed["result"]["required_handoffs"]["wren"])

    def test_game_dev_ownership_and_route(self) -> None:
        game = self.call("shawn_core_query", {"task": "I want to build a game in Unreal Engine"})
        self.assertEqual([item["specialist"] for item in game["specialist_plan"]], ["game-dev", "verifier"])
        self.assertFalse(game["aes_fallback"]["use"])

        modding = self.call("shawn_core_query", {"task": "Fix a Starfield mod crash on startup"})
        self.assertNotIn("game-dev", {item["specialist"] for item in modding["specialist_plan"]})

        pixel_tools = self.call("shawn_core_specialist_tools", {"specialist": "game-dev"})
        self.assertEqual({item["name"] for item in pixel_tools["tools"]}, {"game_dev_capabilities", "game_dev_route"})

        capabilities = self.call("shawn_core_specialist_execute", {"specialist": "game-dev", "tool": "game_dev_capabilities", "arguments_json": "{}"})
        self.assertTrue(any("unreal-mcp" in item for item in capabilities["result"]["does_not_own"]))
        self.assertEqual(capabilities["result"]["inventory"]["summary"]["missing_paths"], 0)

        wiring = self.call("shawn_core_specialist_execute", {"specialist": "game-dev", "tool": "game_dev_route", "arguments_json": json.dumps({"task": "Enable the ModelContextProtocol plugin and wire up .mcp.json for my new project"})})
        self.assertIn("project-wiring", wiring["result"]["lanes"])

        bethesda = self.call("shawn_core_specialist_execute", {"specialist": "game-dev", "tool": "game_dev_route", "arguments_json": json.dumps({"task": "Also touch up an existing Starfield mod while I'm at it"})})
        self.assertTrue(bethesda["result"]["required_handoffs"]["creation-kit"])

    def test_explicit_aliases_and_evaluation_fixture(self) -> None:
        expected = {
            "@nawnie": ("nawnie", "shawn_core_specialist_execute"),
            "@ted": ("ted", "shawn_core_specialist_execute"),
            "@victoria": ("victoria", "shawn_core_specialist_execute"),
            "@wren": ("wren", "shawn_core_specialist_execute"),
            "@cad": ("cad", "shawn_core_specialist_execute"),
            "@corporate-overview": ("corporate-overview", "shawn_core_specialist_execute"),
            "@sensai": ("sensai-advisor", "shawn_core_specialist_execute"),
            "@al": ("al", "shawn_core_specialist_execute"),
            "@carl": ("creation-kit", "shawn_core_specialist_execute"),
            "@token-master": ("token-master", "shawn_core_specialist_execute"),
            "@chrono": ("chrono", "shawn_core_specialist_execute"),
            "@changelog": ("changelog", "shawn_core_specialist_execute"),
            "@operator": ("operator", "shawn_core_specialist_execute"),
            "@pixel": ("game-dev", "shawn_core_specialist_execute"),
        }
        for alias, (specialist, first_tool) in expected.items():
            result = self.call("shawn_core_query", {"task": f"{alias} handle this bounded task"})
            self.assertEqual(result["specialist_plan"][0]["specialist"], specialist)
            self.assertEqual(result["specialist_plan"][0]["first_tool"], first_tool)
            self.assertFalse(result["aes_fallback"]["use"])
        generic = self.call("shawn_core_query", {"task": "Implement a bounded Python 3.12 parser"})
        self.assertTrue(generic["aes_fallback"]["use"])
        evaluation = ET.parse(ROOT / "evals" / "shawn_core_mcp.xml")
        self.assertEqual(len(evaluation.findall("qa_pair")), 13)

    def test_carl_recent_change_dependency_audit_and_route_precedence(self) -> None:
        routed = self.call("shawn_core_query", {"task": "Review recent Starfield mod crash content changes"})
        self.assertEqual([item["specialist"] for item in routed["specialist_plan"]], ["creation-kit"])
        workshop = self.call("carl_enter_workshop", {})
        self.assertTrue(workshop["skill_receipt"]["present"])
        self.assertIn("packaging", workshop["lanes"])
        carl_route = self.call("carl_route", {"task": "Diagnose a startup crash after deploying mods"})
        self.assertEqual(carl_route["first_check"], "carl_creation_kit_audit")

        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            data = root / "Data"
            data.mkdir()
            plugins = root / "Plugins.txt"
            plugins.write_text("*NewestPatch.esp\n", encoding="utf-8")
            master_name = b"InactiveMaster.esm\0"
            payload = b"MAST" + len(master_name).to_bytes(2, "little") + master_name
            payload += b"DATA" + (8).to_bytes(2, "little") + (b"\0" * 8)
            header = b"TES4" + len(payload).to_bytes(4, "little") + (b"\0" * 16)
            (data / "NewestPatch.esp").write_bytes(header + payload)
            (data / "InactiveMaster.esm").write_bytes(header + payload)
            native = data / "SFSE" / "Plugins"
            native.mkdir(parents=True)
            (native / "CrashLogger.dll").write_bytes(b"diagnostic")
            scripts = data / "Scripts"
            scripts.mkdir()
            (scripts / "startup.pex").write_bytes(b"papyrus")

            result = self.call("carl_creation_kit_audit", {
                "game": "starfield", "data_path": str(data), "plugins_path": str(plugins),
            })
            plugin_result = self.call("carl_plugin_audit", {
                "plugin_path": str(data / "NewestPatch.esp"), "data_path": str(data), "plugins_path": str(plugins),
            })
            package_result = self.call("carl_package_audit", {"package_path": str(data)})
        self.assertEqual(result["specialist"], "creation-kit")
        self.assertEqual(result["skill_owner"], "creation-kit")
        self.assertEqual(result["priority_findings"][0]["plugin"], "NewestPatch.esp")
        self.assertEqual(result["priority_findings"][0]["inactive_masters"], ["InactiveMaster.esm"])
        self.assertEqual(result["native_extender_plugins"][0]["classification"], "native_sfse_dll")
        self.assertEqual(result["recent_papyrus_scripts"][0]["classification"], "papyrus_pex_not_native_extender")
        self.assertTrue(any(item["check"] == "recent_changes_first" for item in result["checks"]))
        self.assertEqual(plugin_result["inactive_masters"], ["InactiveMaster.esm"])
        self.assertEqual(package_result["package"]["file_count"], 4)
        self.assertEqual(len(package_result["categories"]["plugins"]), 2)

    def test_execution_returns_only_final_handoff(self) -> None:
        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text("Final handoff: checked files and tests passed.", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="event\n" * 10000, stderr="")

        with patch.object(nawnie_server.subprocess, "run", side_effect=fake_run):
            result = nawnie_server.tool_spawn_agent({"prompt": "Do a bounded check.", "execute": True})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["final_message"], "Final handoff: checked files and tests passed.")
        self.assertNotIn("stdout", result)
        self.assertNotIn("stderr", result)
        self.assertIn("awaited", result["result_capture"])

    def test_bad_model_is_rejected(self) -> None:
        reply = self.rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "nawnie_spawn_agent", "arguments": {"prompt": "test", "model": "unknown"}}})
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("model must be one of", reply["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
