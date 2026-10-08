"""MCP dispatch smoke tests for staged durable repair tools, without Windows services."""
from __future__ import annotations
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nawnie_server as core


class RepairGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"SHAWN_CORE_REPAIR_LEDGER": str(Path(self.temp.name) / "cases.sqlite3")})
        self.env.start()
        self.addCleanup(self.env.stop)

    def call(self, name: str, args: dict) -> dict:
        reply = core.handle({"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":name,"arguments":args}})
        self.assertIn("result", reply)
        return reply["result"]

    def test_registered_tools_and_open_read_resume(self):
        tools = core.handle({"jsonrpc":"2.0","id":1,"method":"tools/list"})["result"]["tools"]
        names = {entry["name"] for entry in tools}
        for name in ("open","note","transition","get","list"):
            self.assertIn("shawn_core_repair_"+name,names)
        opened = self.call("shawn_core_repair_open", {"request_key":"case-qwen-1","project":"Qwen","symptom":"lost result","expected":"answer","actual":"empty","owner":"al"})
        self.assertFalse(opened["isError"], opened)
        cid=opened["structuredContent"]["record"]["case_id"]
        read = self.call("shawn_core_repair_get",{"case_id":cid})
        self.assertEqual(read["structuredContent"]["case"]["status"],"intake")
        transition = self.call("shawn_core_repair_transition",{"case_id":cid,"event_id":"phase-start-1","actor":"al","action":"start","expected_revision":0,"reason":"collect evidence"})
        self.assertFalse(transition["isError"],transition)
        note = self.call("shawn_core_repair_note",{"case_id":cid,"event_id":"observation-1","actor":"al","kind":"observation","body":"tool called; result omitted","expected_revision":1})
        self.assertFalse(note["isError"],note)
        self.assertEqual(self.call("shawn_core_repair_get",{"case_id":cid})["structuredContent"]["case"]["revision"],2)
        self.assertEqual(len(self.call("shawn_core_repair_list",{})["structuredContent"]["cases"]),1)

    def test_preflight_and_permission_claim_fail_closed(self):
        wrong = self.call("shawn_core_repair_open", {"request_key":"a","project":"P","symptom":"s","expected":"e","actual":"a","owner":"verifier"})
        self.assertTrue(wrong["isError"])
        self.assertIn("owner",wrong["content"][0]["text"])
        opened=self.call("shawn_core_repair_open", {"request_key":"case-2","project":"Qwen","symptom":"lost result","expected":"answer","actual":"empty","owner":"al"})
        cid=opened["structuredContent"]["record"]["case_id"]
        wrong_actor=self.call("shawn_core_repair_transition",{"case_id":cid,"event_id":"phase-x","actor":"al","action":"accept_report","expected_revision":0,"reason":"I say passed"})
        self.assertTrue(wrong_actor["isError"])
        self.assertIn("requires status",wrong_actor["content"][0]["text"])

if __name__ == "__main__":
    unittest.main()
