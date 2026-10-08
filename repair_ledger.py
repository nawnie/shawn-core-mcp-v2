"""Durable, append-only repair-case ledger for Shawn Core MCP.

This is a local continuity/control record, NOT an authentication provider, tool
executor, CI verifier, or permission grant. Reported verification is never proof
of a live deployment. The MCP host must authorize the caller separately.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
import os
import re
import sqlite3
import uuid

KINDS = frozenset({"observation", "hypothesis", "probe", "repair", "verification", "lesson"})
ACTIONS = {
    "start": ("intake", "investigating"),
    "plan": ("investigating", "planned"),
    "submit": ("planned", "awaiting_verification"),
    "accept_report": ("awaiting_verification", "verification_reported"),
    "reject_report": ("awaiting_verification", "investigating"),
    "resume": ("blocked", "investigating"),
}
# Minimal defense against accidentally persisting common key types. This is NOT
# comprehensive secret detection: upstream callers must redact logs and tokens.
SECRET = re.compile(r"(?:github_pat_[A-Za-z0-9_]{12,}|gh[pousr]_[A-Za-z0-9]{12,}|sk-[A-Za-z0-9_-]{16,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _field(value: object, name: str, maximum: int, *, optional: bool = False) -> str:
    if optional and value is None:
        return ""
    if not isinstance(value, str) or (not optional and not value.strip()) or len(value) > maximum:
        raise ValueError(f"{name} must be {'a non-empty ' if not optional else 'a '}string <= {maximum} characters")
    if "\x00" in value or SECRET.search(value):
        raise ValueError(f"{name} contains a prohibited credential pattern or NUL")
    return value.strip()


def _revision(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("expected_revision must be a non-negative integer")
    return value


class RepairLedger:
    def __init__(self, path: str | Path | None = None):
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".shawn-core") / "ShawnCore" if os.environ.get("LOCALAPPDATA") else Path.home() / ".shawn-core"
        self.path = Path(path or os.environ.get("SHAWN_CORE_REPAIR_LEDGER", base / "repair-ledger.sqlite3"))
        if self.path.exists() and self.path.is_symlink():
            raise ValueError("repair ledger may not be a symlink")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.parent.is_symlink():
            raise ValueError("repair ledger directory may not be a symlink")
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS cases (
                    case_id TEXT PRIMARY KEY,
                    request_key TEXT UNIQUE NOT NULL,
                    project TEXT NOT NULL,
                    symptom TEXT NOT NULL,
                    expected TEXT NOT NULL,
                    actual TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    status TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL REFERENCES cases(case_id),
                    actor TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    body TEXT NOT NULL,
                    source_ref TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_by_case ON events(case_id, created_at);
            """)
        if os.name != "nt":
            self.path.chmod(0o600)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=5000")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _read(self, db: sqlite3.Connection, case_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if row is None:
            raise ValueError("unknown case_id")
        return row

    def open(self, *, request_key: str, project: str, symptom: str, expected: str, actual: str, owner: str) -> dict:
        values = {
            "request_key": _field(request_key, "request_key", 100),
            "project": _field(project, "project", 120),
            "symptom": _field(symptom, "symptom", 3000),
            "expected": _field(expected, "expected", 2000),
            "actual": _field(actual, "actual", 2000),
            "owner": _field(owner, "owner", 80),
        }
        stamp = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM cases WHERE request_key=?", (values["request_key"],)).fetchone()
            if prior is not None:
                if any(prior[key] != val for key, val in values.items()):
                    raise ValueError("request_key already exists with different intake data")
                return {"case_id": prior["case_id"], "status": prior["status"], "revision": prior["revision"], "reused": True}
            case_id = "repair-" + uuid.uuid4().hex
            db.execute("""INSERT INTO cases
                (case_id, request_key, project, symptom, expected, actual, owner, status, revision, created_at, updated_at)
                VALUES (:case_id, :request_key, :project, :symptom, :expected, :actual, :owner, 'intake', 0, :created, :created)""",
                {**values, "case_id": case_id, "created": stamp})
        return {"case_id": case_id, "status": "intake", "revision": 0, "reused": False}

    def note(self, *, case_id: str, event_id: str, actor: str, kind: str, body: str,
             expected_revision: int, source_ref: str = "") -> dict:
        case_id = _field(case_id, "case_id", 80)
        event_id = _field(event_id, "event_id", 120)
        actor = _field(actor, "actor", 80)
        body = _field(body, "body", 4000)
        source_ref = _field(source_ref, "source_ref", 400, optional=True)
        _revision(expected_revision)
        if kind not in KINDS:
            raise ValueError("unsupported note kind")
        if kind in {"verification", "repair"} and not source_ref:
            raise ValueError(f"{kind} requires a reproducible source_ref (test receipt or change reference)")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
            if prior is not None:
                if any(prior[k] != v for k, v in {"case_id":case_id,"actor":actor,"kind":kind,"body":body,"source_ref":source_ref}.items()):
                    raise ValueError("event_id collision with different content")
                return {"event_id":event_id, "case_id":case_id, "reused":True, "revision":self._read(db,case_id)["revision"]}
            row = self._read(db, case_id)
            if row["revision"] != expected_revision:
                raise ValueError(f"stale revision; current revision is {row['revision']}")
            if row["status"] == "verification_reported" and kind != "lesson":
                raise ValueError("accepted reports are immutable except for learning notes")
            if kind == "verification" and (row["status"] != "awaiting_verification" or actor != "verifier"):
                raise ValueError("verification requires awaiting_verification and actor=verifier")
            if kind == "repair" and row["status"] != "planned":
                raise ValueError("repair evidence must be recorded during planned state")
            stamp = _now()
            db.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?)", (event_id,case_id,actor,kind,body,source_ref,stamp))
            db.execute("UPDATE cases SET revision=revision+1, updated_at=? WHERE case_id=?",(stamp,case_id))
            return {"event_id": event_id, "case_id":case_id, "revision":expected_revision+1, "reused":False}

    def transition(self, *, case_id: str, event_id: str, actor: str, action: str,
                   expected_revision: int, reason: str, new_owner: str = "") -> dict:
        case_id = _field(case_id, "case_id", 80)
        event_id = _field(event_id, "event_id", 120)
        actor = _field(actor, "actor", 80)
        reason = _field(reason, "reason", 2000)
        new_owner = _field(new_owner, "new_owner", 80, optional=True)
        _revision(expected_revision)
        if action not in {*ACTIONS, "block", "handoff"}:
            raise ValueError("unsupported transition")
        if action == "handoff" and not new_owner:
            raise ValueError("handoff requires new_owner")
        if action != "handoff" and new_owner:
            raise ValueError("new_owner is allowed only for handoff")
        body = action + ": " + reason + (" -> " + new_owner if action == "handoff" else "")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
            if prior is not None:
                if (prior["case_id"],prior["actor"],prior["kind"],prior["body"]) != (case_id,actor,"transition",body):
                    raise ValueError("event_id collision with different transition")
                row=self._read(db,case_id)
                return {"case_id":case_id,"status":row["status"],"owner":row["owner"],"revision":row["revision"],"reused":True}
            row = self._read(db, case_id)
            if row["revision"] != expected_revision:
                raise ValueError(f"stale revision; current revision is {row['revision']}")
            status = row["status"]
            owner = row["owner"]
            if action == "handoff":
                if status == "verification_reported":
                    raise ValueError("accepted cases cannot be reassigned")
                owner = new_owner
            elif action == "block":
                if status in {"blocked", "verification_reported"}:
                    raise ValueError("cannot block this status")
                status = "blocked"
            else:
                old, dest = ACTIONS[action]
                if status != old:
                    raise ValueError(f"{action} requires status {old}; current status is {status}")
                if action in {"plan", "submit"} and actor != owner:
                    raise ValueError("implementation owner must submit its own plan/change")
                if action in {"accept_report", "reject_report"} and (actor != "verifier" or owner == "verifier"):
                    raise ValueError("independent verifier report is required")
                counts = dict(db.execute("SELECT kind,COUNT(*) FROM events WHERE case_id=? GROUP BY kind",(case_id,)).fetchall())
                if action == "plan" and not all(counts.get(k,0)>0 for k in ("observation","hypothesis","probe")):
                    raise ValueError("plan requires recorded observation, hypothesis and falsifying probe")
                if action == "submit":
                    last_plan = db.execute("SELECT rowid FROM events WHERE case_id=? AND kind='transition' AND body LIKE 'plan:%' ORDER BY rowid DESC LIMIT 1", (case_id,)).fetchone()
                    check = db.execute("SELECT 1 FROM events WHERE case_id=? AND kind='repair' AND source_ref<>'' AND rowid>? LIMIT 1", (case_id, last_plan[0] if last_plan else 0)).fetchone()
                    if not check:
                        raise ValueError("submit requires a new repair artifact receipt after this plan")
                if action in {"accept_report", "reject_report"}:
                    last_submit = db.execute("SELECT rowid FROM events WHERE case_id=? AND kind='transition' AND body LIKE 'submit:%' ORDER BY rowid DESC LIMIT 1", (case_id,)).fetchone()
                    check = db.execute("SELECT 1 FROM events WHERE case_id=? AND kind='verification' AND actor='verifier' AND source_ref<>'' AND rowid>? LIMIT 1",(case_id,last_submit[0] if last_submit else 0)).fetchone()
                    if not check:
                        raise ValueError("independent verifier receipt missing for current submission")
                status = dest
            stamp=_now()
            db.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?)",(event_id,case_id,actor,"transition",body,"",stamp))
            db.execute("UPDATE cases SET status=?,owner=?,revision=revision+1,updated_at=? WHERE case_id=?",(status,owner,stamp,case_id))
            return {"case_id":case_id,"status":status,"owner":owner,"revision":expected_revision+1,"reused":False}

    def get(self, case_id: str) -> dict:
        case_id = _field(case_id,"case_id",80)
        with self._connect() as db:
            row = self._read(db,case_id)
            events = [dict(event) for event in db.execute("SELECT * FROM events WHERE case_id=? ORDER BY rowid",(case_id,))]
        counts={kind:sum(e["kind"]==kind for e in events) for kind in KINDS}
        return {"case":dict(row),"events":events,"learning":{
            "ready":row["status"]=="verification_reported" and counts["lesson"]>0,
            "missing":(["independent verification report"] if row["status"]!="verification_reported" else []) + (["lesson"] if counts["lesson"]==0 else []),
            "note":"Verifier reports are stored claims with source references, not independently executed by this MCP."
        }}

    def list(self, limit: int = 30) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 to 100")
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT case_id,project,symptom,owner,status,revision,updated_at FROM cases ORDER BY updated_at DESC LIMIT ?",(limit,))]
