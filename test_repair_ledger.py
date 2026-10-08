"""Behavioral and misuse regressions for the persistent repair ledger."""
from __future__ import annotations
import tempfile
import unittest
from pathlib import Path
from repair_ledger import RepairLedger

class RepairLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ledger.sqlite3"
        self.ledger = RepairLedger(self.path)
        self.case = self.ledger.open(request_key="req-001",project="Qwen Chat",symptom="tool output lost",expected="call succeeds",actual="no tool result",owner="al")
        self.cid = self.case["case_id"]

    def event(self, kind, body="test", actor="al", source_ref=""):
        rev=self.ledger.get(self.cid)["case"]["revision"]
        return self.ledger.note(case_id=self.cid,event_id=f"evt-{rev}-{kind}",actor=actor,kind=kind,body=body,source_ref=source_ref,expected_revision=rev)

    def move(self, action, actor="al", reason="transition", new_owner=""):
        rev=self.ledger.get(self.cid)["case"]["revision"]
        return self.ledger.transition(case_id=self.cid,event_id=f"step-{rev}-{action}",actor=actor,action=action,reason=reason,new_owner=new_owner,expected_revision=rev)

    def prepare(self):
        self.move("start")
        self.event("observation","captured raw tool output")
        self.event("hypothesis","parser mismatch")
        self.event("probe","compare raw output and parsed JSON")
        self.move("plan")
        self.event("repair","commit abc123",source_ref="git:abc123")
        self.move("submit")

    def test_open_idempotency_and_restart(self):
        duplicate=self.ledger.open(request_key="req-001",project="Qwen Chat",symptom="tool output lost",expected="call succeeds",actual="no tool result",owner="al")
        self.assertTrue(duplicate["reused"])
        self.assertEqual(duplicate["case_id"],self.cid)
        self.assertEqual(RepairLedger(self.path).get(self.cid)["case"]["status"],"intake")

    def test_reused_request_key_different_payload_is_rejected(self):
        with self.assertRaisesRegex(ValueError,"different intake"):
            self.ledger.open(request_key="req-001",project="Wrong",symptom="tool output lost",expected="call succeeds",actual="no tool result",owner="al")

    def test_plan_requires_evidence_hypothesis_and_probe(self):
        self.move("start")
        with self.assertRaisesRegex(ValueError,"observation"):
            self.move("plan")
        self.event("observation")
        self.event("hypothesis")
        with self.assertRaisesRegex(ValueError,"probe"):
            self.move("plan")

    def test_submitting_without_repair_receipt_is_blocked(self):
        self.move("start")
        for kind in ("observation","hypothesis","probe"):
            self.event(kind)
        self.move("plan")
        with self.assertRaisesRegex(ValueError,"repair artifact"):
            self.move("submit")

    def test_verification_requires_independent_report(self):
        self.prepare()
        with self.assertRaisesRegex(ValueError,"verifier"):
            self.move("accept_report",actor="al")
        with self.assertRaisesRegex(ValueError,"receipt"):
            self.move("accept_report",actor="verifier")
        self.event("verification",actor="verifier",body="original and regression passed",source_ref="ci:run-12")
        reported=self.move("accept_report",actor="verifier")
        self.assertEqual(reported["status"],"verification_reported")
        self.assertFalse(self.ledger.get(self.cid)["learning"]["ready"])
        self.event("lesson",actor="al",body="tool parser, not model, was broken")
        self.assertTrue(self.ledger.get(self.cid)["learning"]["ready"])
        with self.assertRaisesRegex(ValueError,"immutable"):
            self.event("observation")

    def test_verification_note_requires_receipt_and_correct_phase(self):
        with self.assertRaisesRegex(ValueError,"source_ref"):
            self.event("verification",actor="verifier")
        with self.assertRaisesRegex(ValueError,"awaiting_verification"):
            self.event("verification",actor="verifier",source_ref="ci:test")

    def test_rejection_returns_to_investigation(self):
        self.prepare()
        self.event("verification",actor="verifier",body="regression failed",source_ref="ci:run-13")
        result=self.move("reject_report",actor="verifier")
        self.assertEqual(result["status"],"investigating")

    def test_old_verifier_receipt_cannot_approve_new_submission(self):
        self.prepare()
        self.event("verification",actor="verifier",body="failed",source_ref="ci:old")
        self.move("reject_report",actor="verifier")
        self.move("plan")
        with self.assertRaisesRegex(ValueError,"new repair"):
            self.move("submit")
        self.event("repair",body="updated fix",source_ref="git:new")
        self.move("submit")
        with self.assertRaisesRegex(ValueError,"current submission"):
            self.move("accept_report",actor="verifier")

    def test_stale_revision_and_event_id_collision(self):
        result=self.move("start")
        with self.assertRaisesRegex(ValueError,"stale revision"):
            self.ledger.note(case_id=self.cid,event_id="stale-1",actor="al",kind="observation",body="old",expected_revision=0)
        first=self.ledger.note(case_id=self.cid,event_id="evt-idempotent",actor="al",kind="hypothesis",body="hyp",expected_revision=result["revision"])
        second=self.ledger.note(case_id=self.cid,event_id="evt-idempotent",actor="al",kind="hypothesis",body="hyp",expected_revision=0)
        self.assertTrue(second["reused"])
        with self.assertRaisesRegex(ValueError,"collision"):
            self.ledger.note(case_id=self.cid,event_id="evt-idempotent",actor="al",kind="hypothesis",body="different",expected_revision=0)

    def test_handoff_changes_owner_but_not_authority(self):
        self.move("start")
        result=self.move("handoff",actor="nawnie",new_owner="wren")
        self.assertEqual(result["owner"],"wren")
        self.assertEqual(result["status"],"investigating")
        for kind in ("observation","hypothesis","probe"):
            self.event(kind,actor="wren")
        with self.assertRaisesRegex(ValueError,"implementation owner"):
            self.move("plan",actor="al")
        self.move("plan",actor="wren")

    def test_block_resume_keeps_events_and_state(self):
        self.move("start")
        self.event("observation",body="first trace")
        self.move("block",actor="nawnie",reason="needs hardware")
        self.assertEqual(self.move("resume",actor="nawnie")["status"],"investigating")
        self.assertEqual(RepairLedger(self.path).get(self.cid)["events"][1]["body"],"first trace")

    def test_credentials_are_rejected(self):
        with self.assertRaisesRegex(ValueError,"credential"):
            self.event("observation",body="log: github_pat_ABCDEFGHIJKLMN")

    def test_unknown_case_does_not_create_new_case(self):
        with self.assertRaisesRegex(ValueError,"unknown case_id"):
            self.ledger.get("repair-nope")
        self.assertEqual(len(self.ledger.list()),1)

    def test_no_preverified_claim_from_open(self):
        case=self.ledger.get(self.cid)
        self.assertFalse(case["learning"]["ready"])
        self.assertIn("not independently executed",case["learning"]["note"])

if __name__ == "__main__":
    unittest.main()
