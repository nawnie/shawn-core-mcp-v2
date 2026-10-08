#!/usr/bin/env python3
"""Nawnie's local newline-delimited MCP server.

The server is deliberately a control plane. It routes work through the pack's
existing orchestrator, gives the host structured Ted/Victoria/Agent T handoffs,
and can launch an explicitly requested, read-only Codex subagent. It never
silently starts a model run, publishes, spends, changes credentials, or opens a
network port.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from port_policy import PortPolicyError, validate_port_request
from agent_diagnostics import diagnose, preflight_tool_call
from specialist_agents import agent_profile, agent_prompt
from repair_ledger import RepairLedger


SERVER_NAME = "shawn-core"
SERVER_VERSION = "0.8.1"
PROTOCOL_VERSION = "2025-11-25"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
ROUTER = PLUGIN_ROOT / "skills" / "orchestrator" / "scripts" / "route_prompt.py"
INSTALLED_SKILL_ROUTER = PLUGIN_ROOT / "skills" / "orchestrator" / "scripts" / "installed_skill_router.py"
CREATION_KIT_SKILL = Path.home() / ".codex" / "skills" / "creation-kit" / "SKILL.md"
CODEX_CONFIG = Path.home() / ".codex" / "config.toml"
MAX_PROMPT_CHARS = 12_000
MAX_TIMEOUT_SECONDS = 300
ALLOWED_MODELS = {"gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-sol-wm", "gpt-5.6-terra"}
ALLOWED_REASONING = {"low", "medium", "high", "xhigh", "max", "ultra"}
MODEL_INVENTORY = Path("F:/Ai_Models/AIWF/model_inventory.json")
RESEARCH_ROOTS = (Path("F:/datasets"), Path("F:/AI-Data"))
MECHANICAL_WREN_SESSION_HOME = Path(os.environ.get(
    "SHAWN_CORE_CONTINUITY_ROOT",
    str(Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ShawnCore" / "continuity"),
)) / "mechanical-wren-sessions"
SPECIALIST_TOOL_REGISTRY = Path(os.environ.get(
    "SHAWN_CORE_TOOL_REGISTRY",
    "F:/Shawn-Core/registry/SPECIALIST_TOOL_REGISTRY.json",
))
MAX_RESEARCH_MANIFESTS = 50
MAX_FINAL_MESSAGE_CHARS = 8_000
MAX_FAILURE_DIAGNOSTIC_CHARS = 2_000
CHILD_MCP_TIMEOUT_SECONDS = 120
OPERATOR_ALLOWED_ROOTS = (
    Path("C:/Users/Shawn/Documents/Codex"),
    Path("D:/Codex-Projects/Desktop"),
    Path("F:/AI-Agent-Workspace"),
    Path("F:/_Projects"),
)
OPERATOR_BLOCKED_TERMS = (
    "remove-item", "del ", "erase ", " rmdir", " rd ", "format ",
    "restart-computer", "stop-computer", "shutdown", "set-executionpolicy",
    "new-netfirewallrule", "remove-netfirewallrule", "add-mppreference",
    "invoke-webrequest", "curl ", "start-bitstransfer", "start-process",
)

# These are fixed, local installed runtimes. Shawn Core resolves only bundled
# sibling plugins or known Codex cache roots; callers cannot supply an
# executable, cwd, module, or server URL.
def _resolve_plugin_root(name: str, preferred_version: str) -> Path:
    suite_root = os.environ.get("SHAWN_CORE_PLUGIN_ROOT")
    candidates: list[Path] = []

    def add_container(container: Path) -> None:
        candidates.append(container / preferred_version)
        if container.is_dir():
            candidates.extend(
                sorted(
                    (path for path in container.iterdir() if path.is_dir()),
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )
            )
        # Some test or development roots point directly at an unpacked plugin.
        candidates.append(container)

    if suite_root:
        add_container(Path(suite_root) / name)
    # PLUGIN_ROOT is .../cache/<marketplace>/nawnie/<version>. Resolve sibling
    # plugins from the marketplace directory so redirected HOME/CODEX_HOME
    # values cannot hide installed child specialists.
    add_container(PLUGIN_ROOT.parents[1] / name)
    for cache_name in ("personal", "shawn-local-plugins"):
        plugin_cache = Path.home() / ".codex" / "plugins" / "cache" / cache_name / name
        add_container(plugin_cache)
    return next(
        (
            path
            for path in candidates
            if path.is_dir() and (path / ".codex-plugin" / "plugin.json").is_file()
        ),
        candidates[0],
    )


CHILD_MCP_ROUTES: dict[str, tuple[Path, tuple[str, ...]]] = {
    "ted": (_resolve_plugin_root("ted-cfo", "0.2.0+codex.20260828225924"), ("mcp/ted_server.py",)),
    "victoria": (_resolve_plugin_root("victoria-marketing-seo", "0.1.0+codex.20260731102949"), ("mcp/victoria_server.py",)),
    "agent-t": (_resolve_plugin_root("agent-t", "0.2.0+codex.20260813083306"), ("mcp/agent_t_server.py",)),
    "corporate-overview": (_resolve_plugin_root("shawn-core-specialist-suite", "0.1.1"), ("mcp/core_specialists_server.py",)),
    "sensai-advisor": (_resolve_plugin_root("shawn-core-specialist-suite", "0.1.1"), ("mcp/core_specialists_server.py",)),
    "verifier": (_resolve_plugin_root("shawn-core-specialist-suite", "0.1.1"), ("mcp/core_specialists_server.py",)),
    "researcher": (_resolve_plugin_root("shawn-core-specialist-suite", "0.1.1"), ("mcp/core_specialists_server.py",)),
    "legal-readiness": (_resolve_plugin_root("shawn-core-specialist-suite", "0.1.1"), ("mcp/core_specialists_server.py",)),
    "wren": (_resolve_plugin_root("wren-site-builder", "0.1.0+codex.20260828225755"), ("mcp/wren_server.py",)),
    "creation-kit": (_resolve_plugin_root("creation-kit", "0.3.0+codex.20260831014944"), ("mcp/creation_kit_server.py",)),
    "cad": (Path("F:/Shawn-Core/current/specialists/cad/implementation"), ("server.py",)),
}
if not CREATION_KIT_SKILL.is_file():
    CREATION_KIT_SKILL = CHILD_MCP_ROUTES["creation-kit"][0] / "skills" / "creation-kit" / "SKILL.md"

SPECIALISTS: dict[str, dict[str, Any]] = {
    "nawnie": {"aliases": ("@nawnie", "nawnie"), "role": "whole-problem orchestrator: decomposition, routing, redistribution, conflict resolution, and synthesis", "mcp_server": "shawn-core", "first_tool": "shawn_core_query", "tool_contract": "Nawnie may do bounded fallback work, but does not absorb a specialist's implementation lane or become the default coder for every repository."},
    "ted": {"aliases": ("@ted", "ted", "ted-cfo"), "role": "finance, legal research, business economics, and CFO controls", "mcp_server": "ted-cfo", "first_tool": "ted_enter_shed", "host_capabilities": ("web research", "spreadsheets", "documents")},
    "victoria": {"aliases": ("@victoria", "victoria", "victoria-marketing-seo"), "role": "marketing, SEO, campaigns, discovery, and Victoria Web App operations", "mcp_server": "victoria-marketing-seo", "first_tool": "victoria_enter_desk", "tool_contract": "Use Victoria's own 18 MCP tools; do not replace them with generic AES skills."},
    "agent-t": {"aliases": ("@agent-t", "agent-t", "agent t"), "role": "cybersecurity, compliance readiness, and production assurance", "mcp_server": "agent-t", "first_tool": "agent_t_status"},
    "corporate-overview": {"display_name": "Corporate overview", "aliases": ("@corporate-overview", "@pgv", "corporate overview", "corporate-overview", "pgv"), "role": "hospitality, property, acquisition, construction, and operations", "mcp_server": "shawn-core-specialist-suite", "first_tool": "corporate_overview_enter_desk", "legacy_aliases": ("pgv",)},
    "sensai-advisor": {"display_name": "Sensai", "aliases": ("@sensai", "@sensai-advisor", "@rocky", "sensai", "sensai advisor", "sensai-advisor", "rocky", "rocky advisor"), "role": "opt-in owner and operator advisory inference", "mcp_server": "shawn-core-specialist-suite", "first_tool": "sensai_advisor_enter_desk", "opt_in_only": True, "legacy_aliases": ("rocky", "rocky-advisor")},
    "verifier": {"aliases": ("@verifier", "verifier"), "role": "independent falsification, recomputation, regression review, and acceptance; never the implementation owner", "mcp_server": "shawn-core-specialist-suite", "first_tool": "verifier_enter_desk"},
    "researcher": {"aliases": ("@researcher", "researcher"), "role": "evidence research, source grading, and canonical proposals", "mcp_server": "shawn-core-specialist-suite", "first_tool": "researcher_enter_desk"},
    "legal-readiness": {"aliases": ("@legal-readiness", "legal readiness"), "role": "document readiness and licensed-counsel handoff", "mcp_server": "shawn-core-specialist-suite", "first_tool": "legal_readiness_enter_desk"},
    "wren": {"aliases": ("@wren", "wren"), "role": "web UX, site and briefing-page information architecture, addressable records, frontend quality, and product-facing API integration", "mcp_server": "wren-site-builder", "first_tool": "wren_enter_desk", "tool_contract": "Wren owns browser-facing integration and frontend contracts. General backend services, databases, infrastructure, and AI inference internals route outward."},
    "cad": {"aliases": ("@cad", "cad", "cad specialist"), "role": "CAD modeling, manufacturable geometry, assemblies, drawings, and export verification", "mcp_server": "cad-specialist", "first_tool": "cad.capabilities", "tool_contract": "Use CAD Specialist's own 21 MCP tools for bounded modeling, assembly, drawing, and export work. Nawnie owns cross-specialist synthesis and acceptance."},
    "game-dev": {"display_name": "Pixel", "aliases": ("@game-dev", "@pixel", "game dev", "game-dev", "game dev specialist", "pixel"), "role": "game engine readiness, project wiring, and asset-pipeline coordination across Unreal, Unity, and Godot", "mcp_server": "shawn-core", "first_tool": "game_dev_capabilities", "aes_skill": "game-dev", "tool_contract": "Pixel owns engine and toolchain readiness, per-project MCP wiring guidance, and asset handoff between CAD/Blender and a game engine. It never duplicates a live engine-editor bridge (for example the installed unreal-mcp skill) and never claims runtime health beyond registered path presence. Bethesda Creation Kit modding of an existing shipped game stays with Creation Kit/Carl; Pixel owns original engine projects."},
    "al": {"aliases": ("@al", "al specialist"), "role": "AI systems and inference engineering: CUDA and GPU behavior, LLM runtimes, model serving, training and inference, AI-specific FastAPI services, data contracts, tests, and harness integration", "mcp_server": "shawn-core", "first_tool": "al_capabilities", "aes_skill": "ai-ml-specialist", "tool_contract": "AL owns AI-system decisions and proof gates. General application backends do not route to AL merely because they use Python, FastAPI, a database, or an API."},
    "operator": {"aliases": ("@operator", "operator specialist", "terminal specialist", "file editing specialist", "small scripting specialist", "batch specialist"), "role": "permission-aware terminal and file execution plus bounded operational BAT, PowerShell, and one-off scripting", "mcp_server": "shawn-core", "first_tool": "operator_capabilities", "tool_contract": "Operator executes a domain owner's bounded plan. It may handle small operational scripts, but does not design large Python, JavaScript, or TypeScript codebases and never becomes the domain or architecture owner."},
    "creation-kit": {"aliases": ("@creation-kit", "creation-kit", "creation kit", "@carl", "carl", "creation kit specialist"), "role": "Archive-first Bethesda authoring, TES records, Papyrus, assets, BMKB, Vortex management, runtime verification, packaging, restoration, and release readiness. Carl is the internal router.", "mcp_server": "creation-kit-mcp", "first_tool": "creation_kit_enter", "tool_contract": "Use the canonical Creation Kit MCP tools. Translate legacy carl_* calls to creation_kit_* without duplicating implementation logic."},
    "mechanical-engineer": {"aliases": ("@mechanical", "@mechanical-engineer", "mechanical engineer"), "role": "AI-device hardware architecture, mechanical requirements, enclosure and thermal concepts, manufacturability, and verification planning", "mcp_server": "shawn-core", "first_tool": "mechanical_wren_session", "tool_contract": "Runs a persisted two-way planning session with Wren. Mechanical Engineer owns hardware requirements; Wren owns the human-facing site and briefing translation. Nawnie owns synthesis and acceptance."},
    "token-master": {"aliases": ("@token-master", "token master", "token-master"), "role": "Codex token efficiency, context retention, compaction policy research, and evidence-backed configuration recommendations", "mcp_server": "shawn-core", "first_tool": "token_master_audit", "tool_contract": "Read-only by default. Token Master may recommend a bounded host change, but does not modify model context ceilings, compaction limits, or MCP enablement without an explicit follow-up request."},
    "chrono": {"aliases": ("@chrono", "chrono", "project planner", "plan mode"), "role": "project planning, dependency sequencing, contingent execution branches, and plan-level acceptance gates", "mcp_server": "shawn-core", "first_tool": "chrono_plan", "tool_contract": "Chrono owns the final Core plan. Every Core plan-mode request runs one bounded, side-effect-free reasoning round: Nawnie supplies the whole-problem route, Token Master supplies the live context and MCP-efficiency receipt, and Chrono synthesizes the executable contingent plan. This is structured deliberation, not a claim that the Codex host launched independent model agents."},
    "changelog": {"aliases": ("@changelog", "changelog specialist", "worklog specialist"), "role": "start-of-task continuity intake and end-of-task receipt-backed changelog preparation", "mcp_server": "shawn-core", "first_tool": "changelog_intake", "tool_contract": "Uses Atlas Cartographer, the local Git environment, and @github handoffs. Its Luna subprocess policy is 64,000-token compaction and a 100,000-token ceiling; the Codex host must enforce those limits."},
}

MODDING_TERMS = (
    "creation kit", "starfield mod", "skyrim mod", "fallout mod", "mod crash",
    "modding", "mods", "load order", "plugins.txt", "plugin master", "sfse",
    "skse", "f4se", "papyrus", "vortex", ".esp", ".esm", ".esl", "ba2", "bsa",
)
GAME_EXTENDERS = {
    "starfield": ("SFSE", {"starfield.esm"}),
    "skyrimse": ("SKSE", {"skyrim.esm", "update.esm"}),
    "fallout4": ("F4SE", {"fallout4.esm"}),
}
CARL_LANES = {
    "authoring": {"signals": ("create", "author", "quest", "npc", "outfit", "navmesh", "worldspace", "papyrus"), "owner": "Creation Kit", "gate": "dedicated development profile and disposable test save"},
    "conflict": {"signals": ("conflict", "override", "winning record", "xedit", "master"), "owner": "xEdit", "gate": "plugin header, master graph, and winning-record inspection"},
    "deployment": {"signals": ("install", "enable", "disable", "deploy", "purge", "load order", "vortex", "mo2"), "owner": "Vortex or MO2", "gate": "manager ownership and active-profile verification"},
    "crash": {"signals": ("crash", "ctd", "startup", "won't launch", "wont launch", "freeze"), "owner": "Carl failure isolation", "gate": "recent deployed changes and declared masters before runtime speculation"},
    "modlist": {"signals": ("wabbajack", "modlist", "portable mo2"), "owner": "Wabbajack plus portable MO2", "gate": "clean reproducible instance and exact game build"},
    "packaging": {"signals": ("package", "ba2", "bsa", "archive", "release mod"), "owner": "Carl packaging audit", "gate": "plugin, generated outputs, archive paths, hashes, and rollback receipt"},
}


class NawnieError(ValueError):
    """A safe, user-correctable MCP tool error."""


def hidden_subprocess_kwargs() -> dict[str, Any]:
    """Suppress consoles for every child of the configured pythonw host."""
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def _has_signal(text: str, signal: str) -> bool:
    """Match a routing phrase, never a substring or an email's @ suffix."""
    if signal.startswith("@"):
        pattern = r"(?<![\w@])" + re.escape(signal) + r"(?![\w-])"
    else:
        # File extensions follow a filename; hyphenated domain words still count.
        pattern = ("" if signal.startswith(".") else r"(?<!\w)") + re.escape(signal) + r"(?!\w)"
    return re.search(pattern, text) is not None


def _response_detail(args: dict[str, Any], *, compact: bool = False) -> str:
    allowed = {"full", "compact" if compact else "summary"}
    detail = args.get("detail", "full")
    if not isinstance(detail, str) or detail not in allowed:
        raise NawnieError("detail must be one of: " + ", ".join(sorted(allowed)))
    return detail


def schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def require_string(args: dict[str, Any], name: str, *, max_length: int = MAX_PROMPT_CHARS) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise NawnieError(f"{name} must be a non-empty string")
    value = value.strip()
    if len(value) > max_length:
        raise NawnieError(f"{name} exceeds the {max_length}-character limit")
    return value


def canonical_specialist_id(value: str) -> str:
    normalized = value.strip().casefold().lstrip("@")
    aliases = {
        alias.casefold().lstrip("@"): specialist_id
        for specialist_id, spec in SPECIALISTS.items()
        for alias in spec["aliases"]
    }
    return aliases.get(normalized, normalized)


def canonical_tool_name(value: str) -> str:
    for legacy_prefix, canonical_prefix in (("pgv_", "corporate_overview_"), ("rocky_advisor_", "sensai_advisor_"), ("carl_", "creation_kit_")):
        if value.startswith(legacy_prefix):
            return canonical_prefix + value[len(legacy_prefix):]
    return value


def local_plugin_names() -> list[str]:
    return [str(item["name"]).split("@", 1)[0] for item in codex_plugin_installations() if item["status"] == "installed, enabled"]


def codex_plugin_installations() -> list[dict[str, str | None]]:
    """Read the live Codex plugin registry, not merely source folders on disk."""
    try:
        completed = subprocess.run(
            ["codex", "plugin", "list"], text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False,
            **hidden_subprocess_kwargs(),
        )
    except OSError:
        return []
    if completed.returncode != 0:
        return []
    records: list[dict[str, str | None]] = []
    for line in completed.stdout.splitlines():
        cells = re.split(r"\s{2,}", line.strip(), maxsplit=3)
        if not cells or "@" not in cells[0] or len(cells) < 2:
            continue
        status = cells[1]
        if status not in {"installed, enabled", "installed, disabled", "not installed"}:
            continue
        version = cells[2] if len(cells) >= 3 and cells[2] else None
        path = cells[3] if len(cells) >= 4 else None
        records.append({"name": cells[0], "status": status, "version": version, "path": path})
    return records


def installed_skill_catalog() -> dict[str, Any] | None:
    """Return the Nawnie router's live skill inventory summary when available."""
    local_skills = sorted(path.name for path in (PLUGIN_ROOT / "skills").iterdir() if path.is_dir())
    fallback = {
        "source": "gateway-local-fallback",
        "unique_skills": len(local_skills),
        "skills": local_skills,
        "boundary": "The host-wide inventory was unavailable; this lists only skills bundled with the active gateway.",
    }
    if not INSTALLED_SKILL_ROUTER.is_file():
        return fallback
    try:
        completed = subprocess.run(
            [sys.executable, str(INSTALLED_SKILL_ROUTER), "inventory"], cwd=PLUGIN_ROOT,
            text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=5, check=False,
            **hidden_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return fallback
    if completed.returncode != 0:
        return fallback
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return fallback
    return result if isinstance(result, dict) else fallback


def file_receipt(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "bytes": stat.st_size,
        "modified_utc": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def safe_path_exists(path: str | Path) -> bool:
    """Treat inaccessible or untrusted Windows mount points as unavailable."""
    try:
        return Path(path).exists()
    except OSError:
        return False


def tool_core_tool_inventory(args: dict[str, Any]) -> dict[str, Any]:
    """Read and recheck the bounded machine-local specialist tool registry."""
    if SPECIALIST_TOOL_REGISTRY.is_file():
        try:
            registry = json.loads(SPECIALIST_TOOL_REGISTRY.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise NawnieError(f"Specialist tool registry is unreadable: {exc}") from exc
        registry_receipt: dict[str, Any] = file_receipt(SPECIALIST_TOOL_REGISTRY)
    else:
        specialists: dict[str, Any] = {
            "al": {
                "status": "gateway-native",
                "tools": [
                    {
                        "id": "ai-ml-specialist",
                        "path": str(PLUGIN_ROOT / "skills" / "ai-ml-specialist" / "SKILL.md"),
                        "exists": (PLUGIN_ROOT / "skills" / "ai-ml-specialist" / "SKILL.md").is_file(),
                    }
                ],
            }
        }
        for owner, (root, entry) in CHILD_MCP_ROUTES.items():
            specialists[owner] = {
                "status": "registered-child-mcp",
                "tools": [{"id": f"{owner}-mcp", "path": str(root / entry[0]), "exists": (root / entry[0]).is_file()}],
            }
        registry = {"machine": os.environ.get("COMPUTERNAME"), "captured_utc": None, "specialists": specialists}
        registry_receipt = {"path": str(SPECIALIST_TOOL_REGISTRY), "present": False, "fallback": "gateway allowlist"}
    specialists = registry.get("specialists")
    if not isinstance(specialists, dict):
        raise NawnieError("Specialist tool registry has no specialists object")
    if "al" not in specialists:
        specialists = dict(specialists)
        specialists["al"] = {
            "status": "gateway-native",
            "tools": [
                {
                    "id": "ai-ml-specialist",
                    "path": str(PLUGIN_ROOT / "skills" / "ai-ml-specialist" / "SKILL.md"),
                    "exists": (PLUGIN_ROOT / "skills" / "ai-ml-specialist" / "SKILL.md").is_file(),
                }
            ],
        }
    specialist_id = args.get("specialist_id")
    selected = specialists
    if specialist_id:
        if not isinstance(specialist_id, str):
            raise NawnieError(f"Unknown specialist_id. Choose from: {', '.join(sorted(specialists))}")
        specialist_id = canonical_specialist_id(specialist_id)
        if specialist_id not in specialists:
            raise NawnieError(f"Unknown specialist_id. Choose from: {', '.join(sorted(specialists))}")
        selected = {specialist_id: specialists[specialist_id]}
    verified: dict[str, Any] = {}
    present_count = 0
    missing_count = 0
    for owner, record in selected.items():
        item = dict(record) if isinstance(record, dict) else {"status": "invalid-record"}
        checked_tools = []
        for tool_record in item.get("tools", []):
            checked = dict(tool_record)
            path_raw = checked.get("path")
            live_present = safe_path_exists(path_raw) if isinstance(path_raw, str) and path_raw else False
            checked["live_present"] = live_present
            checked["registry_presence_matches"] = checked.get("exists") is live_present
            present_count += int(live_present)
            missing_count += int(not live_present)
            checked_tools.append(checked)
        item["tools"] = checked_tools
        verified[owner] = item
    return {
        "schema": "shawn-core.specialist-tool-inventory-response.v1",
        "registry": registry_receipt,
        "captured_utc": registry.get("captured_utc"),
        "machine": registry.get("machine"),
        "specialists": verified,
        "summary": {"specialists": len(verified), "present_paths": present_count, "missing_paths": missing_count},
        "boundary": "Read-only bounded path recheck. Presence does not prove runtime health, compatibility, authentication, or successful tool use.",
    }


def tool_al_capabilities(_args: dict[str, Any]) -> dict[str, Any]:
    """Describe AL's owned AI-system lanes without overstating runtime health."""
    inventory = tool_core_tool_inventory({"specialist_id": "al"})
    return {
        "schema": "shawn-core.al-capabilities.v1",
        "specialist": "al",
        "owns": [
            "CUDA, GPU, PyTorch, and model-runtime behavior",
            "LLM loading, quantization, training, evaluation, inference, and serving",
            "AI-specific FastAPI services, workers, queues, streaming, and data contracts",
            "local AI harness integration and deterministic performance or compatibility gates",
        ],
        "does_not_own": [
            "general application backends merely implemented in Python or FastAPI",
            "browser-facing UX and product API integration owned by Wren",
            "terminal and file execution owned by Operator",
            "cross-domain routing, conflict resolution, or final synthesis owned by Nawnie",
            "independent acceptance owned by Verifier",
        ],
        "collaboration": {
            "wren": "Owns the browser-facing contract; AL owns inference semantics and model-serving behavior behind it.",
            "operator": "Executes bounded local file, terminal, BAT, PowerShell, or one-off script work when the active harness lacks native tools.",
            "verifier": "Independently checks acceptance criteria, regression risk, and receipts.",
            "nawnie": "Decomposes the whole task, resolves overlap, and synthesizes delivery.",
        },
        "inventory": inventory,
        "boundary": "Registered paths and planning lanes are callable. They do not prove CUDA, model, FastAPI, or serving runtime health until task-specific checks run.",
    }


def tool_al_route(args: dict[str, Any]) -> dict[str, Any]:
    """Classify an AI engineering task into AL-owned lanes and handoff gates."""
    task = require_string(args, "task")
    lowered = task.casefold()
    lanes: list[str] = []
    if any(term in lowered for term in ("cuda", "cudnn", "gpu kernel", "torch", "pytorch", "triton", "onnxruntime", "vram")):
        lanes.append("cuda-gpu-runtime")
    if any(term in lowered for term in ("llm", "model serving", "inference", "ollama", "vllm", "llama.cpp", "llama-server", "transformers")):
        lanes.append("llm-inference-serving")
    if any(term in lowered for term in ("training", "fine-tun", "lora", "qlora", "quantization", "evaluation", "eval harness")):
        lanes.append("training-quantization-evaluation")
    ai_signal = bool(lanes) or any(term in lowered for term in ("ai model", "machine learning", "model runtime", "model endpoint"))
    if ai_signal and any(term in lowered for term in ("fastapi", "inference api", "model api", "streaming endpoint", "worker queue")):
        lanes.append("ai-fastapi-service")
    if ai_signal and any(term in lowered for term in ("data contract", "schema", "migration", "database", "harness", "openclaw", "hermes")):
        lanes.append("ai-data-and-harness-integration")
    lanes = list(dict.fromkeys(lanes))
    return {
        "schema": "shawn-core.al-route.v1",
        "specialist": "al",
        "task": task,
        "owned": bool(lanes),
        "lanes": lanes,
        "required_handoffs": {
            "wren": any(term in lowered for term in ("frontend", "browser", "web ui", "product-facing api", "client integration")),
            "operator": any(term in lowered for term in ("powershell", ".ps1", ".bat", "batch file", "terminal command", "one-off script")),
            "verifier": True,
        },
        "proof_gates": [
            "inventory presence is not runtime compatibility",
            "record exact environment, model, device, endpoint, and test command",
            "measure GPU or serving claims rather than infer them from configuration",
            "return implementation and deterministic receipts to Verifier and Shawn Core validation",
        ],
        "boundary": "If no AI-owned lane is selected, route the general backend or application task through Nawnie/AES rather than assigning it to AL.",
    }


def require_local_path(args: dict[str, Any], name: str, *, directory: bool) -> Path:
    raw = require_string(args, name, max_length=1_000)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise NawnieError(f"{name} must be an absolute local path")
    try:
        path = path.resolve(strict=True)
    except OSError as exc:
        raise NawnieError(f"{name} does not exist: {path}") from exc
    if directory != path.is_dir():
        expected = "directory" if directory else "file"
        raise NawnieError(f"{name} must be an existing {expected}")
    return path


def _operator_path(raw: str, *, must_exist: bool) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise NawnieError("path must be an absolute local path")
    try:
        resolved = path.resolve(strict=must_exist)
    except OSError as exc:
        raise NawnieError(f"path cannot be resolved: {path}") from exc
    if not any(resolved.is_relative_to(root) for root in OPERATOR_ALLOWED_ROOTS):
        raise NawnieError("path is outside the operator's approved local roots")
    return resolved


def _operator_permission(args: dict[str, Any], action: str) -> None:
    if args.get("permission_grant") != "user-authorized-local":
        raise NawnieError(f"{action} requires permission_grant=user-authorized-local")


def tool_operator_capabilities(_args: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "shawn-core.operator-capabilities.v1",
        "specialist": "operator",
        "approved_roots": [str(item) for item in OPERATOR_ALLOWED_ROOTS],
        "tools": {
            "operator_file": "Read UTF-8 text, or atomically write UTF-8 text with a user-authorized-local grant. Delete, rename, and recursive operations are not supported.",
            "operator_terminal": "Plan a PowerShell command by default. Execution requires user-authorized-local, an approved working directory, and a bounded 300-second timeout. This includes bounded BAT, PowerShell, and one-off operational scripting.",
        },
        "blocked": "Deletion, recursive removal, shutdown, persistent execution-policy changes, firewall changes, Defender exclusions, downloads, background process launches, credentials, public exposure, and any path outside the approved roots.",
        "boundary": "This provides a local capability bridge for harnesses that lack file or terminal tools. Operator executes a domain owner's bounded plan and does not design or own large Python, JavaScript, or TypeScript codebases. The calling harness must explicitly declare the user's local permission before a mutation or command is executed.",
    }


def tool_operator_file(args: dict[str, Any]) -> dict[str, Any]:
    action = args.get("action", "read")
    if action not in {"read", "write"}:
        raise NawnieError("action must be read or write")
    raw_path = require_string(args, "path", max_length=1_000)
    path = _operator_path(raw_path, must_exist=action == "read")
    if action == "read":
        if not path.is_file():
            raise NawnieError("path must be a regular file")
        text = path.read_text(encoding="utf-8", errors="replace")
        return {"specialist": "operator", "action": "read", "path": str(path), "content": text[:131_072], "truncated": len(text) > 131_072, "sha256": sha256_file(path)}
    _operator_permission(args, "file write")
    content = args.get("content")
    if not isinstance(content, str) or len(content) > 1_000_000:
        raise NawnieError("content must be a UTF-8 string of at most 1,000,000 characters")
    if not path.parent.is_dir():
        raise NawnieError("the destination parent directory must already exist")
    expected = args.get("expected_sha256")
    if expected is not None and (not path.exists() or sha256_file(path) != str(expected).upper()):
        raise NawnieError("expected_sha256 does not match the current file")
    temporary = path.with_name(f".{path.name}.operator-{os.getpid()}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)
    return {"specialist": "operator", "action": "write", "path": str(path), "bytes": path.stat().st_size, "sha256": sha256_file(path), "rollback": "Restore the prior text from the caller's source control or backup. This tool does not delete or retain file copies."}


def tool_operator_terminal(args: dict[str, Any]) -> dict[str, Any]:
    command = require_string(args, "command", max_length=8_000)
    working_directory = _operator_path(require_string(args, "working_directory", max_length=1_000), must_exist=True)
    if not working_directory.is_dir():
        raise NawnieError("working_directory must be a directory")
    timeout = args.get("timeout_seconds", 120)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
        raise NawnieError(f"timeout_seconds must be an integer between 1 and {MAX_TIMEOUT_SECONDS}")
    lowered = command.casefold()
    if any(term in lowered for term in OPERATOR_BLOCKED_TERMS):
        raise NawnieError("command contains a blocked operation class")
    plan = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
    if args.get("execute") is not True:
        return {"specialist": "operator", "status": "planned", "working_directory": str(working_directory), "command": plan[:-1] + ["<command>"], "timeout_seconds": timeout, "permission_required": "user-authorized-local"}
    _operator_permission(args, "terminal execution")
    completed = subprocess.run(plan, cwd=working_directory, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False, **hidden_subprocess_kwargs())
    return {"specialist": "operator", "status": "completed" if completed.returncode == 0 else "failed", "working_directory": str(working_directory), "exit_code": completed.returncode, "stdout": completed.stdout[-16_000:], "stderr": completed.stderr[-16_000:], "output_truncated": len(completed.stdout) > 16_000 or len(completed.stderr) > 16_000}


def plugin_masters(path: Path) -> list[str]:
    """Read MAST subrecords from a Bethesda TES4 plugin header."""
    with path.open("rb") as stream:
        header = stream.read(24)
        if len(header) != 24 or header[:4] != b"TES4":
            raise NawnieError(f"plugin does not start with a TES4 record: {path}")
        payload_size = int.from_bytes(header[4:8], "little")
        if payload_size > 64 * 1024 * 1024:
            raise NawnieError(f"TES4 header is unreasonably large: {path}")
        payload = stream.read(payload_size)
    if len(payload) != payload_size:
        raise NawnieError(f"TES4 header is truncated: {path}")
    masters: list[str] = []
    offset = 0
    extended_size: int | None = None
    while offset + 6 <= len(payload):
        signature = payload[offset:offset + 4]
        size = int.from_bytes(payload[offset + 4:offset + 6], "little")
        offset += 6
        if signature == b"XXXX":
            if size != 4 or offset + size > len(payload):
                raise NawnieError(f"invalid XXXX subrecord in {path}")
            extended_size = int.from_bytes(payload[offset:offset + 4], "little")
            offset += size
            continue
        actual_size = extended_size if extended_size is not None else size
        extended_size = None
        if offset + actual_size > len(payload):
            raise NawnieError(f"truncated {signature!r} subrecord in {path}")
        value = payload[offset:offset + actual_size]
        offset += actual_size
        if signature == b"MAST":
            masters.append(value.split(b"\0", 1)[0].decode("utf-8", errors="replace"))
    return masters


def active_plugins(path: Path) -> tuple[set[str], list[str]]:
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    active = {line[1:].strip().casefold() for line in lines if line.startswith("*") and line[1:].strip()}
    return active, lines


def model_manifest_status() -> dict[str, Any]:
    if not MODEL_INVENTORY.is_file():
        return {"present": False, "path": str(MODEL_INVENTORY)}
    try:
        inventory = json.loads(MODEL_INVENTORY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"present": True, **file_receipt(MODEL_INVENTORY), "parse_error": str(exc)}
    assets = inventory.get("assets", []) if isinstance(inventory, dict) else []
    families: dict[str, int] = {}
    architectures: dict[str, int] = {}
    for asset in assets if isinstance(assets, list) else []:
        if not isinstance(asset, dict):
            continue
        family = asset.get("family")
        architecture = asset.get("architecture")
        if isinstance(family, str):
            families[family] = families.get(family, 0) + 1
        if isinstance(architecture, str):
            architectures[architecture] = architectures.get(architecture, 0) + 1
    return {
        "present": True,
        **file_receipt(MODEL_INVENTORY),
        "asset_count": len(assets) if isinstance(assets, list) else 0,
        "families": dict(sorted(families.items())),
        "architectures": dict(sorted(architectures.items())),
        "boundary": "Inventory metadata only. This does not load weights, inspect model binaries, or prove runtime compatibility.",
    }


def research_manifest_status(query: str | None = None) -> dict[str, Any]:
    candidates: list[Path] = []
    query_folded = query.casefold() if query else None
    for root in RESEARCH_ROOTS:
        if not root.is_dir():
            continue
        for directory, _, filenames in os.walk(root):
            for filename in filenames:
                lowered = filename.casefold()
                if not (lowered == "manifest.json" or lowered.endswith(".manifest.json")):
                    continue
                path = Path(directory) / filename
                if query_folded and query_folded not in str(path).casefold():
                    continue
                candidates.append(path)
    candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    recent = [file_receipt(path) for path in candidates[:MAX_RESEARCH_MANIFESTS]]
    return {
        "roots": [str(root) for root in RESEARCH_ROOTS if root.is_dir()],
        "manifest_count": len(candidates),
        "query": query,
        "recent_manifests": recent,
        "boundary": "Manifest filenames and file metadata only. Dataset records and research contents are not read.",
    }


def command_recommendations(task: str, *, include_route: bool = True) -> list[dict[str, Any]]:
    """Choose Nawnie MCP commands before outside agents or model execution."""
    lowered = task.casefold()
    commands: list[dict[str, Any]] = []
    if any(phrase in lowered for phrase in ("token budget", "token use", "token usage", "token limit", "context length", "context window", "compaction", "compression limit", "compress context")):
        commands.append({"tool": "token_master_audit", "arguments": {}, "reason": "Inspect the active Codex configuration and resident MCP footprint before recommending any token or context change."})
    if any(term in lowered for term in MODDING_TERMS):
        commands.append({"tool": "carl_creation_kit_audit", "arguments": {"game": "<starfield|skyrimse|fallout4>", "data_path": "<absolute Data directory>", "plugins_path": "<absolute Plugins.txt>"}, "reason": "Carl starts Bethesda crash diagnosis from recent deployed changes and validates every declared master before runtime speculation."})
    if any(word in lowered for word in ("model", "weights", "quant", "gguf", "safetensors", "diffusers", "loader", "vram", "ollama", "vllm", "llama.cpp", "llama-server")):
        commands.append({"tool": "nawnie_manifest_status", "arguments": {"scope": "model"}, "reason": "Check the authoritative F: model inventory before choosing a loader or model route."})
    if any(word in lowered for word in ("research", "dataset", "corpus", "manifest", "training data", "findings")):
        commands.append({"tool": "nawnie_manifest_status", "arguments": {"scope": "research"}, "reason": "Check F: research manifest metadata before curation, training, or research routing."})
    if any(word in lowered for word in ("installed", "plugin", "skill", "available", "codex setup")):
        commands.append({"tool": "nawnie_status", "arguments": {}, "reason": "Use the live Codex install registry rather than assume a plugin or skill is available."})
    if any(term in lowered for term in ("fix agent", "agent failed", "agent keeps", "debug llm", "fix mcp", "tool call error", "wrong tool", "malformed arguments", "tool failure")):
        commands.insert(0, {"tool": "shawn_core_diagnose", "arguments": {"symptom": task}, "reason": "First diagnose the failing layer and propose falsifiable probes before changing models."})
    if include_route:
        commands.append({"tool": "nawnie_route", "arguments": {"task": task}, "reason": "Apply Nawnie's skill and specialist routing after relevant local state is known."})
    if any(word in lowered for word in ("compare", "versus", "vs ", "benchmark models")):
        commands.append({"tool": "nawnie_compare", "arguments": {"prompt": "<task-specific comparison prompt>", "execute": False}, "reason": "First produce a bounded, read-only comparison plan."})
    elif any(word in lowered for word in ("run agent", "spawn agent", "use sol", "ask sol", "subagent")):
        commands.append({"tool": "nawnie_spawn_agent", "arguments": {"prompt": "<task-specific prompt>", "execute": False}, "reason": "First produce a read-only execution plan; it does not start a model until execute=true."})
    return commands


def specialist_handoff(specialist_id: str, task: str, *, reason: str | None = None, explicit: bool = False) -> dict[str, Any]:
    spec = SPECIALISTS[specialist_id]
    result: dict[str, Any] = {
        "specialist": specialist_id,
        "display_name": spec.get("display_name", specialist_id),
        "role": spec["role"],
        "mcp_server": "shawn-core",
        "first_tool": "shawn_core_specialist_execute",
        "agent_entrypoint": "shawn_core_specialist_agent",
        "specialist_tool": spec["first_tool"],
        "task": task,
        "reason": reason or "Explicitly named specialist.",
        "explicit": explicit,
    }
    for key in ("host_capabilities", "tool_contract", "opt_in_only", "aes_skill"):
        if key in spec:
            result[key] = spec[key]
    return result


def _implementation_requested(lowered: str) -> bool:
    if any(phrase in lowered for phrase in (
        "implement", "fix", "patch", "debug", "refactor", "write code", "edit code",
        "create a script", "small script", "bat script", "batch script", ".bat", ".ps1", "powershell command",
        "add an endpoint", "integrate the api",
    )):
        return True
    return "build" in lowered and any(term in lowered for term in (
        "website", "frontend", "backend", "api", "service", "application", "app", "game",
        "react", "vue", "javascript", "typescript", "python", "cuda", "model", "llm",
    ))


def _append_handoff(handoffs: list[dict[str, Any]], specialist_id: str, task: str, *, reason: str) -> None:
    if any(item["specialist"] == specialist_id for item in handoffs):
        return
    handoffs.append(specialist_handoff(specialist_id, task, reason=reason))


def specialist_handoffs(task: str) -> list[dict[str, Any]]:
    lowered = task.casefold()
    explicit = [specialist_id for specialist_id, spec in SPECIALISTS.items() if any(alias.startswith("@") and _has_signal(lowered, alias) for alias in spec["aliases"])]
    if explicit:
        handoffs = [specialist_handoff(item, task, explicit=True) for item in explicit]
        if _implementation_requested(lowered) and "verifier" not in explicit:
            _append_handoff(handoffs, "verifier", task, reason="Independent acceptance and regression lane for an implementation task.")
        return handoffs
    handoffs: list[dict[str, Any]] = []
    if any(_has_signal(lowered, term) for term in MODDING_TERMS):
        return [specialist_handoff("creation-kit", task, reason="Creation Kit MCP owns Bethesda work through Carl's internal router; archive and dependency gates run before mutation or runtime claims.")]
    if any(_has_signal(lowered, phrase) for phrase in ("token budget", "token use", "token usage", "token limit", "context length", "context window", "compaction", "compression limit", "compress context")):
        handoffs.append(specialist_handoff("token-master", task, reason="Token and context efficiency needs a focused, evidence-backed audit. Nawnie remains the whole-problem owner."))
    financial_text = re.sub(r"\b(?:token|time|memory|context|vram|gpu) budgets?\b", "", lowered)
    if any(_has_signal(financial_text, word) for word in ("finance", "budget", "ledger", "tax", "funding", "legal", "compliance")):
        handoffs.append(specialist_handoff("ted", task, reason="Finance, legal research, funding, or economics ownership."))
    if any(_has_signal(lowered, word) for word in ("marketing", "seo", "campaign", "website content", "social content", "content strategy", "ranking", "sitemap")):
        handoffs.append(specialist_handoff("victoria", task, reason="Marketing, SEO, discovery, campaign, or content ownership."))
    if any(_has_signal(lowered, word) for word in ("security", "security audit", "threat", "vulnerability", "release readiness", "production release", "publish", "deploy")):
        handoffs.append(specialist_handoff("agent-t", task, reason="Security, readiness, audit, or release evidence ownership."))
    if any(_has_signal(lowered, word) for word in ("property", "hotel", "hospitality", "acquisition", "rehab", "room inventory")):
        handoffs.append(specialist_handoff("corporate-overview", task, reason="Property and hospitality operating ownership."))
    ai_signals = any(_has_signal(lowered, term) for term in (
        "ai model", "machine learning", "training model", "inference", "local models",
        "ollama", "vllm", "llama.cpp", "llama-server", "openclaw", "hearmes",
        "hermes", "llm", "cuda", "cudnn", "gpu kernel", "pytorch", "torch",
        "transformers", "quantization", "model serving", "model endpoint",
    ))
    ai_service_signals = ai_signals and any(_has_signal(lowered, term) for term in (
        "fastapi", "api", "data contract", "database schema", "api schema",
        "migration", "worker", "queue", "streaming", "harness",
    ))
    if ai_signals or ai_service_signals:
        _append_handoff(handoffs, "al", task, reason="AI systems, CUDA/GPU, LLM runtime, model-serving, or AI-specific FastAPI ownership.")
    if any(_has_signal(lowered, phrase) for phrase in (
        "file edit", "edit files", "powershell", "terminal command", "shell command",
        "terminal specialist", "batch file", ".bat", ".cmd", ".ps1", "one-off script",
        "small script", "small python script", "small javascript script", "small typescript script",
    )):
        _append_handoff(handoffs, "operator", task, reason="Permission-aware terminal/file execution or bounded operational scripting under the domain owner's plan.")
    if any(_has_signal(lowered, phrase) for phrase in ("ai device", "hardware requirements", "mechanical engineering", "enclosure", "thermal", "heat sink", "manufacturability", "bom")):
        handoffs.append(specialist_handoff("mechanical-engineer", task, reason="AI-device physical hardware and manufacturability ownership. Use the Mechanical Engineer and Wren session tool when the product story, briefing page, or visual communication also needs design decisions."))
    if any(_has_signal(lowered, phrase) for phrase in ("cad", "3d model", "3d print", "assembly drawing", "technical drawing", "step export", "stl export", "dxf export")):
        handoffs.append(specialist_handoff("cad", task, reason="CAD modeling, assembly, drawing, or export ownership."))
    if any(_has_signal(lowered, phrase) for phrase in ("unreal engine", "unreal editor", ".uproject", "unity engine", "godot engine", "game engine", "gameplay", "level design", "game dev", "video game", "make a game", "build a game", "game prototype")):
        handoffs.append(specialist_handoff("game-dev", task, reason="Game engine readiness, project wiring, or asset-pipeline ownership."))
    if any(_has_signal(lowered, phrase) for phrase in (
        "build the website", "publish the site", "briefing page", "frontend design",
        "web ux", "site structure", "site navigation", "frontend quality", "react frontend",
        "frontend api integration", "product-facing api", "browser api integration", "client api integration",
    )):
        _append_handoff(handoffs, "wren", task, reason="Web UX, site information architecture, frontend quality, or product-facing API integration ownership.")
    if _implementation_requested(lowered):
        _append_handoff(handoffs, "verifier", task, reason="Independent acceptance and regression lane for an implementation task.")
    return handoffs


def _specialist_phases(task: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    handoffs = specialist_handoffs(task)
    active = handoffs[:4]
    # Keep acceptance in the active phase; retain every other owner for later work.
    verifier = next((item for item in handoffs if item["specialist"] == "verifier"), None)
    if verifier is not None and verifier not in active:
        active = handoffs[:3] + [verifier]
    return active, [item for item in handoffs if item not in active]


def route(task: str) -> dict[str, Any]:
    if not ROUTER.is_file():
        raise NawnieError(f"Nawnie router is missing: {ROUTER}")
    completed = subprocess.run(
        [sys.executable, str(ROUTER), "--prompt", task],
        cwd=PLUGIN_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
        check=False,
        **hidden_subprocess_kwargs(),
    )
    if completed.returncode != 0:
        raise NawnieError(f"Nawnie router failed: {completed.stderr.strip() or completed.returncode}")
    try:
        routed = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise NawnieError(f"Nawnie router returned invalid JSON: {exc}") from exc
    active, deferred = _specialist_phases(task)
    return {
        "task": task,
        "route": routed,
        "specialist_handoffs": active,
        "deferred_specialists": deferred,
        "recommended_nawnie_commands": command_recommendations(task, include_route=False),
        "execution_boundary": "Routing and handoffs are immediate. Model execution requires execute=true.",
    }


def child_handoff_prompt(prompt: str) -> str:
    """Ask the child for one useful handoff, not a progress transcript."""
    return (
        "Complete this bounded task. Return one concise, self-contained final handoff "
        "with the result, files changed (if any), validation receipts, and blockers. "
        "Do not include progress narration or a tool-event transcript.\n\n"
        f"Task:\n{prompt}"
    )


def build_agent_command(args: dict[str, Any], final_message_path: Path | None = None) -> tuple[list[str], Path, int, dict[str, Any]]:
    prompt = require_string(args, "prompt")
    model = args.get("model", "gpt-5.6-sol")
    reasoning = args.get("reasoning_effort", "medium")
    timeout = args.get("timeout_seconds", 120)
    cwd_raw = args.get("cwd", str(PLUGIN_ROOT))
    if model not in ALLOWED_MODELS:
        raise NawnieError(f"model must be one of: {', '.join(sorted(ALLOWED_MODELS))}")
    if reasoning not in ALLOWED_REASONING:
        raise NawnieError(f"reasoning_effort must be one of: {', '.join(sorted(ALLOWED_REASONING))}")
    if model == "gpt-5.6-luna" and reasoning == "ultra":
        raise NawnieError("gpt-5.6-luna supports reasoning_effort through max, not ultra")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
        raise NawnieError(f"timeout_seconds must be an integer from 1 to {MAX_TIMEOUT_SECONDS}")
    if not isinstance(cwd_raw, str):
        raise NawnieError("cwd must be an absolute directory path")
    cwd = Path(cwd_raw).expanduser().resolve()
    if not cwd.is_dir() or not cwd.is_absolute():
        raise NawnieError("cwd must be an existing absolute directory")
    executable = os.environ.get("NAWNIE_CODEX_EXECUTABLE", "codex")
    final_output = final_message_path or Path("<final-message>")
    command = [
        executable, "exec", "--ephemeral", "--model", model,
        "--sandbox", "read-only", "--cd", str(cwd),
        "-c", f'model_reasoning_effort="{reasoning}"',
        "--output-last-message", str(final_output), child_handoff_prompt(prompt),
    ]
    display_command = command.copy()
    display_command[display_command.index("--output-last-message") + 1] = "<final-message>"
    display_command[-1] = "<prompt>"
    plan = {
        "model": model,
        "reasoning_effort": reasoning,
        "sandbox": "read-only",
        "cwd": str(cwd),
        "timeout_seconds": timeout,
        "command": display_command,
        "prompt_chars": len(prompt),
        "result_capture": "Wait for child exit, then return only its bounded final message. Child event output is discarded.",
        "max_final_message_chars": MAX_FINAL_MESSAGE_CHARS,
    }
    return command, cwd, timeout, plan


def core_request_id(task: str, acceptance: list[str]) -> str:
    digest = hashlib.sha256((task + "\0" + "\0".join(acceptance)).encode("utf-8")).hexdigest()
    return f"core-{digest[:16]}"


def core_routing_mermaid(
    skills: list[dict[str, Any]],
    specialists: list[dict[str, Any]],
    *,
    aes_fallback: bool,
    plan_mode: bool = False,
) -> str:
    """Produce a user-renderable graph without embedding untrusted task text."""
    lines = [
        "flowchart LR",
        '    U["User request"] --> N["Nawnie: whole-problem owner"]',
        '    N --> R["Route and capability decomposition"]',
    ]
    if plan_mode:
        lines.extend([
            '    R --> C["Chrono: plan owner"]',
            '    C --> T["Token Master: context and MCP efficiency audit"]',
            '    T --> C',
            '    C --> P["Contingent execution plan"]',
        ])
    if skills:
        lines.append('    subgraph A["Parallel analysis lanes"]')
        for index, item in enumerate(skills, start=1):
            name = str(item.get("skill", f"skill-{index}")).replace('"', "'")
            lines.append(f'        A{index}["{name}"]')
            lines.append(f"        R --> A{index}")
        lines.append("    end")
    if specialists:
        lines.append('    subgraph S["Parallel specialist lanes"]')
        for index, item in enumerate(specialists, start=1):
            name = str(item.get("specialist", f"specialist-{index}")).replace('"', "'")
            lines.append(f'        S{index}["{name}"]')
            lines.append(f"        R --> S{index}")
            if name == "operator":
                lines.append(f'        S{index} --> G{index}{{"User permission and path gate"}} --> E{index}["Evidence and receipts"]')
            elif name == "al":
                lines.append(f'        S{index} --> G{index}{{"Runtime and compatibility proof"}} --> E{index}["Evidence and receipts"]')
            else:
                lines.append(f"        S{index} --> E{index}[\"Evidence and receipts\"]")
        lines.append("    end")
        for index in range(1, len(specialists) + 1):
            lines.append(f"    E{index} --> V")
    if aes_fallback:
        lines.append(('    P' if plan_mode else '    R') + ' --> F["AES implementation fallback"] --> V')
    for index in range(1, len(skills) + 1):
        lines.append(f"    A{index} --> V")
    if plan_mode:
        lines.append("    P --> V")
    if not specialists and not skills and not aes_fallback:
        lines.append(("    P" if plan_mode else "    R") + " --> V")
    lines.extend([
        '    V{"Deterministic validation"}',
        '    V -->|pass| D["Accepted delivery"]',
        '    V -->|missing proof or failure| N',
    ])
    return "\n".join(lines)


def tool_core_context(_args: dict[str, Any]) -> dict[str, Any]:
    specialists = {
        key: {item: value for item, value in spec.items() if item not in {"aliases", "mcp_server", "first_tool"}}
        | {"aliases": list(spec["aliases"]), "mcp_server": "shawn-core", "first_tool": "shawn_core_specialist_execute", "specialist_tool": spec["first_tool"]}
        for key, spec in SPECIALISTS.items()
    }
    return {
        "server": SERVER_NAME,
        "orchestrator": "nawnie",
        "specialists": specialists,
        "general_capability_pack": "AES",
        "activation_policy": "Always-on personal gateway unless Shawn explicitly opts out for this task.",
        "execution_policy": "Core owns routing and receipt validation. Authorized host tools, installed skills, and connectors execute capability gaps under Nawnie; routing is advisory and does not grant permissions or intercept host calls.",
        "legacy_name_policy": "AIWF is compatibility-only for existing paths, package names, schemas, and code identifiers. New user-facing capability names use AES.",
        "flow": ["query", "Nawnie route", "specialist or AES execution", "Core validation", "accept or return to Nawnie for redistribution"],
        "server_creation_policy": "Call shawn_core_port_validate before creating or launching any local TCP server. Use its assigned port and revalidate immediately before launch.",
    }


def tool_core_port_validate(args: dict[str, Any]) -> dict[str, Any]:
    service_name = require_string(args, "service_name", max_length=160)
    preferred_port = args.get("preferred_port")
    range_start = args.get("range_start", preferred_port)
    range_end = args.get("range_end", preferred_port)
    if any(not isinstance(value, int) or isinstance(value, bool) for value in (preferred_port, range_start, range_end)):
        raise NawnieError("preferred_port, range_start, and range_end must be integers")
    reservation_name = args.get("reservation_name")
    if reservation_name is not None and (not isinstance(reservation_name, str) or not reservation_name.strip()):
        raise NawnieError("reservation_name must be a non-empty string when supplied")
    try:
        return validate_port_request(
            service_name=service_name,
            preferred_port=preferred_port,
            range_start=range_start,
            range_end=range_end,
            bind=str(args.get("bind", "loopback")),
            reservation_name=reservation_name.strip() if isinstance(reservation_name, str) else None,
        )
    except PortPolicyError as exc:
        raise NawnieError(str(exc)) from exc


def _plan_mode(args: dict[str, Any], task: str) -> bool:
    """Recognize only explicit Core plan-mode calls and the legacy /plan prefix."""
    mode = args.get("mode")
    if mode is None:
        return task.casefold().lstrip().startswith("/plan")
    if mode not in {"execute", "plan"}:
        raise NawnieError("mode must be execute or plan")
    return mode == "plan"


def _planning_lists(args: dict[str, Any]) -> tuple[list[str], list[str]]:
    acceptance = args.get("acceptance_criteria", [])
    constraints = args.get("constraints", [])
    if not isinstance(acceptance, list) or not all(isinstance(item, str) and item.strip() for item in acceptance):
        raise NawnieError("acceptance_criteria must be an array of non-empty strings")
    if not isinstance(constraints, list) or not all(isinstance(item, str) and item.strip() for item in constraints):
        raise NawnieError("constraints must be an array of non-empty strings")
    return acceptance, constraints


def _distinct_specialists(handoffs: list[dict[str, Any]]) -> list[str]:
    return list(dict.fromkeys(item["specialist"] for item in handoffs if item["specialist"] != "verifier"))


def tool_chrono_plan(args: dict[str, Any]) -> dict[str, Any]:
    """Run the bounded Nawnie, Token Master, and Chrono planning round."""
    task = require_string(args, "task")
    acceptance, constraints = _planning_lists(args)
    routed = route(task)
    execution_specialists = _distinct_specialists(routed["specialist_handoffs"] + routed["deferred_specialists"])
    execution_skills = [item["skill"] for item in routed["route"].get("selected", []) if isinstance(item, dict) and isinstance(item.get("skill"), str)]
    token_receipt = tool_token_master_audit({})
    owner_labels = execution_specialists or execution_skills or ["AES implementation fallback"]
    implementation_gate = "Run each named owner only after the authoritative project root, local instructions, and current working state are verified."
    validation_gate = "Run the narrow deterministic checks for each changed surface, preserve receipts, and return failures to Nawnie for redistribution."
    active_plan = [
        {"phase": "scope", "owner": "nawnie", "action": "Confirm the task, constraints, acceptance criteria, authoritative project root, and capability route before edits.", "gate": "Do not start implementation from a stale path, assumed runtime, or missing project instruction."},
        {"phase": "implementation", "owner": owner_labels, "action": "Execute only the selected owner lanes in dependency order, reusing the existing repository architecture and avoiding speculative parallel edits.", "gate": implementation_gate},
        {"phase": "validation", "owner": "verifier" if any(item["specialist"] == "verifier" for item in routed["specialist_handoffs"]) else "nawnie", "action": "Validate the requested outcome with deterministic checks, exact configuration or runtime smoke evidence where applicable, and receipt-backed acceptance.", "gate": validation_gate},
    ]
    contingencies = [
        {"branch": "ready", "when": "The project root, instructions, inputs, and required capability are present.", "next": "Follow the active plan in dependency order."},
        {"branch": "input-gap", "when": "The authoritative project root, required input, or acceptance criterion is missing or ambiguous.", "next": "Stop before mutation, record the exact missing item, and let Nawnie request or discover only that bounded input."},
        {"branch": "capability-gap", "when": "The selected owner is unavailable, its fixed tool path fails, or the route lacks a required capability.", "next": "Preserve the route receipt and have Nawnie redistribute the unresolved phase to the smallest registered owner or AES fallback."},
        {"branch": "validation-failure", "when": "A deterministic check, exact-config smoke, or independent acceptance gate fails.", "next": "Keep completed evidence, isolate the failing surface, and return to Nawnie for one bounded repair route instead of repeating the same plan."},
    ]
    return {
        "schema": "shawn-core.chrono-plan.v1",
        "specialist": "chrono",
        "task": task,
        "acceptance_criteria": acceptance,
        "constraints": constraints,
        "reasoning_round": {
            "participants": ["nawnie", "token-master", "chrono"],
            "round_count": 1,
            "side_effects": False,
            "child_agents_started": 0,
            "nawnie": {"contribution": "whole-problem route, specialist handoffs, and selected AES skills", "route": routed["route"], "specialist_handoffs": routed["specialist_handoffs"], "deferred_specialists": routed["deferred_specialists"]},
            "token_master": {"contribution": "live Codex context and resident MCP efficiency receipt", "audit": token_receipt},
            "chrono": {"contribution": "owned dependency-ordered plan, contingencies, and validation gates"},
            "boundary": "The round is a deterministic, structured deliberation. It does not claim to start independent model agents or change Codex host limits."},
        "active_plan": active_plan,
        "contingency_model": {"strategy": "Each phase has a ready, input-gap, capability-gap, and validation-failure branch. Branches compose, but only the current branch executes, so coverage grows without speculative work.", "branches": contingencies},
        "efficiency_policy": {"token_master_receipt_required": True, "max_reasoning_rounds": 1, "reuse_route_receipt": True, "parallelism": "Parallelize only independent specialist analysis after Nawnie confirms no shared write surface.", "redistribution": "Do not repeat a failed path without a changed hypothesis or newly available evidence."},
        "next_action": "Execute the active branch only. Return deterministic receipts to shawn_core_validate; Nawnie owns any redistribution.",
    }


def tool_core_query(args: dict[str, Any]) -> dict[str, Any]:
    task = require_string(args, "task")
    acceptance, constraints = _planning_lists(args)
    detail = _response_detail(args, compact=True)
    plan_mode = _plan_mode(args, task)
    if plan_mode:
        chrono_plan = tool_chrono_plan({"task": task, "acceptance_criteria": acceptance, "constraints": constraints})
        routed = chrono_plan["reasoning_round"]["nawnie"]
        specialists = [specialist_handoff("chrono", task, reason="Core plan mode gives Chrono final planning ownership."), specialist_handoff("token-master", task, reason="Every Chrono plan round includes the live context and MCP-efficiency audit.")]
        aes_fallback = not bool(_distinct_specialists(routed["specialist_handoffs"]))
        next_action = "Chrono has emitted the active plan and contingencies. Execute only the active branch, then return deterministic receipts to shawn_core_validate."
    else:
        routed = route(task)
        specialists = routed["specialist_handoffs"]
        aes_fallback = not bool([item for item in specialists if item["specialist"] != "verifier"])
        chrono_plan = None
        next_action = "Use shawn_core_specialist_tools and shawn_core_specialist_execute for each selected specialist through this server, or execute the AES skill route. Return the bounded result to shawn_core_validate."
    result = {
        "schema": "shawn-core.request.v1",
        "request_id": core_request_id(task, acceptance),
        "task": task,
        "mode": "plan" if plan_mode else "execute",
        "constraints": constraints,
        "acceptance_criteria": acceptance,
        "nawnie": {"owns": "whole problem, route, intervention, redistribution, and synthesis", "skill_route": routed["route"]},
        "routing_visualization": {"format": "mermaid", "mermaid": core_routing_mermaid(routed["route"].get("selected", []), specialists, aes_fallback=aes_fallback, plan_mode=plan_mode), "rendering_policy": "Show this graph when requested or when it clarifies cross-domain dependencies. Skills and specialist lanes may run in parallel when their dependencies permit; validation returns failures to Nawnie for redistribution."},
        "specialist_plan": specialists,
        "deferred_specialists": routed.get("deferred_specialists", []),
        "capability_fallback": "If an owner lacks a callable capability, use authorized host tools, installed skills or connectors under Nawnie and return receipts. An empty route is not a refusal; identify the missing capability and continue safe local work.",
        "changelog_intake": tool_changelog_intake({"task": task}),
        "aes_fallback": {"use": aes_fallback, "skills": routed["route"].get("selected", []), "name": "AES"},
        "validation_contract": {"return_tool": "shawn_core_validate", "deterministic_checks_override_confidence": True, "verifier_is_independent": True, "implementation_owner_must_not_self_verify": True},
        "server_creation_gate": {"required_for_tcp_servers": True, "tool": "shawn_core_port_validate", "rule": "Use the assigned port; never kill an existing owner or silently fall through to an arbitrary framework-selected port."},
        "next_action": next_action,
    }
    if chrono_plan is not None:
        result["chrono_plan"] = chrono_plan
    result["detail"] = detail
    if result["deferred_specialists"]:
        result["next_action"] += " Complete deferred_specialists in later phases; do not silently drop their scope."
    if detail == "compact":
        result.pop("routing_visualization")
        result.pop("changelog_intake")
        for key in ("specialist_plan", "deferred_specialists"):
            result[key] = [{field: item[field] for field in ("specialist", "specialist_tool", "first_tool", "reason", "explicit") if field in item} for item in result[key]]
    return result


def tool_core_validate(args: dict[str, Any]) -> dict[str, Any]:
    request_id = require_string(args, "request_id", max_length=80)
    owner = require_string(args, "owner", max_length=80)
    status = args.get("status")
    if status not in {"completed", "partial", "blocked", "failed"}:
        raise NawnieError("status must be completed, partial, blocked, or failed")
    evidence = args.get("evidence", [])
    receipts = args.get("receipts", [])
    blockers = args.get("blockers", [])
    for name, value in (("evidence", evidence), ("receipts", receipts), ("blockers", blockers)):
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise NawnieError(f"{name} must be an array of strings")
    accepted = status == "completed" and args.get("acceptance_met") is True and args.get("deterministic_checks_passed") is True and bool(receipts)
    next_specialist = args.get("recommended_next_specialist")
    if next_specialist is not None:
        if not isinstance(next_specialist, str):
            raise NawnieError("recommended_next_specialist is not registered in Shawn Core")
        next_specialist = canonical_specialist_id(next_specialist)
        if next_specialist not in SPECIALISTS:
            raise NawnieError("recommended_next_specialist is not registered in Shawn Core")
    return {
        "schema": "shawn-core.validation.v1",
        "request_id": request_id,
        "owner": owner,
        "outcome": "accepted" if accepted else "redistribute",
        "accepted": accepted,
        "evidence_count": len(evidence),
        "receipt_count": len(receipts),
        "blockers": blockers,
        "nawnie_intervention_required": not accepted,
        "recommended_next_specialist": next_specialist,
        "nawnie_action": "Synthesize the accepted result." if accepted else "Reassess the missing capability, preserve completed work and receipts, and redistribute the unresolved phase.",
        "independent_verifier": "optional after acceptance; required when the task requests independent proof" if accepted else "defer until the owning phase can satisfy deterministic gates",
    }


def tool_status(_args: dict[str, Any]) -> dict[str, Any]:
    skills = sorted(path.name for path in (PLUGIN_ROOT / "skills").iterdir() if path.is_dir())
    plugins = codex_plugin_installations()
    enabled = [item for item in plugins if item["status"] == "installed, enabled"]
    disabled = [item for item in plugins if item["status"] == "installed, disabled"]
    enabled_names = {str(item["name"]).split("@", 1)[0] for item in enabled}
    return {
        "server": SERVER_NAME,
        "version": SERVER_VERSION,
        "plugin_root": str(PLUGIN_ROOT),
        "router_present": ROUTER.is_file(),
        "skill_count": len(skills),
        "implicit_router": "orchestrator" if "orchestrator" in skills else None,
        "known_specialists": sorted(SPECIALISTS),
        "codex_plugins": {"enabled": enabled, "disabled": disabled},
        "installed_skill_catalog": installed_skill_catalog(),
        "manifest_sources": {
            "model_inventory": str(MODEL_INVENTORY),
            "research_roots": [str(root) for root in RESEARCH_ROOTS],
        },
        "model_routes": sorted(ALLOWED_MODELS),
        "execution_policy": "Agent runs are disabled by default and, when explicitly enabled, use read-only sandboxing.",
    }


def _toml_scalar(text: str, key: str) -> str | None:
    match = re.search(rf"(?m)^\s*{re.escape(key)}\s*=\s*(.+?)\s*(?:#.*)?$", text)
    return match.group(1) if match else None


def _mcp_enabled(text: str, name: str) -> bool | None:
    match = re.search(rf"(?ms)^\[mcp_servers\.{re.escape(name)}\]\s*(.*?)(?=^\[|\Z)", text)
    if not match:
        return None
    value = _toml_scalar(match.group(1), "enabled")
    return value != "false"


def tool_token_master_audit(_args: dict[str, Any]) -> dict[str, Any]:
    """Report the live, bounded configuration facts Token Master needs to advise."""
    if not CODEX_CONFIG.is_file():
        return {"specialist": "token-master", "config_present": False, "config_path": str(CODEX_CONFIG)}
    text = CODEX_CONFIG.read_text(encoding="utf-8", errors="replace")
    resident_servers = {
        name: _mcp_enabled(text, name)
        for name in ("shawn-core", "comfy-local", "node_repl", "context7", "vortex-local-control")
    }
    return {
        "schema": "shawn-core.token-master-audit.v1",
        "specialist": "token-master",
        "config_path": str(CODEX_CONFIG),
        "config_sha256": sha256_file(CODEX_CONFIG),
        "active_facts": {
            "default_model": _toml_scalar(text, "model"),
            "compact_scope": _toml_scalar(text, "model_auto_compact_token_limit_scope"),
            "shawn_core_always_enabled": resident_servers["shawn-core"],
            "resident_mcp_servers": resident_servers,
        },
        "assessment": [
            "Nawnie remains token-aware for routine work and does not need Token Master for every task.",
            "Token Master is the co-router for evidence-backed token, context, and compaction investigations.",
            "Provider model context ceilings are observed limits, not a host setting that this MCP may raise.",
        ],
        "change_authority": "recommendations only. This tool does not edit Codex configuration, change context ceilings, or toggle MCP servers.",
        "next_step": "Use this receipt to research a specific optimization, then request a narrowly scoped host change if one is warranted.",
    }


def tool_changelog_intake(args: dict[str, Any]) -> dict[str, Any]:
    task = require_string(args, "task")
    execute = args.get("execute", False)
    if not isinstance(execute, bool):
        raise NawnieError("execute must be a boolean")
    result = {
        "schema": "shawn-core.changelog-intake.v1",
        "specialist": "changelog",
        "phase": "start",
        "task": task,
        "subprocess_policy": {"model": "gpt-5.6-luna", "compact_at_tokens": 64000, "hard_context_limit_tokens": 100000, "boundary": "A host-side runner must enforce these values. They do not alter a provider context window."},
        "core_tools": [
            {"owner": "atlas-cartographer", "purpose": "retrieve or write a compact continuity card"},
            {"owner": "personal-git", "purpose": "read the verified local branch, status, and commit history"},
            {"owner": "@github", "purpose": "read repository, issue, pull-request, and remote commit context when available"},
        ],
        "end_of_task": "Return verified changes and receipts to the Changelog specialist before final delivery.",
    }
    if execute:
        result["subprocess"] = tool_spawn_agent({
            "prompt": "Perform only the Changelog startup intake for this task. Retrieve concise relevant continuity, Git, and GitHub handoff needs. Do not edit files.\n\n" + task,
            "model": "gpt-5.6-luna",
            "reasoning_effort": "low",
            "timeout_seconds": args.get("timeout_seconds", 120),
            "cwd": args.get("cwd", str(PLUGIN_ROOT)),
            "execute": True,
        })
    else:
        result["subprocess"] = {"status": "planned", "next_step": "Set execute=true to run the read-only Luna startup intake."}
    return result


def tool_changelog_finalize(args: dict[str, Any]) -> dict[str, Any]:
    """Render a receipt-backed handoff for the end of a bounded task."""
    title = require_string(args, "title")
    changes = args.get("changes", [])
    receipts = args.get("receipts", [])
    blockers = args.get("blockers", [])
    for key, value in {"changes": changes, "receipts": receipts, "blockers": blockers}.items():
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            raise NawnieError(f"{key} must be an array of non-empty strings")
    if not receipts:
        raise NawnieError("receipts must contain at least one verified receipt")
    return {
        "schema": "shawn-core.changelog-finalize.v1",
        "specialist": "changelog",
        "phase": "end",
        "title": title,
        "changes": changes,
        "receipts": receipts,
        "blockers": blockers,
        "core_tools": ["atlas-cartographer", "personal-git", "@github"],
        "delivery_gate": "Record the final compact continuity card and preserve Git and GitHub receipts before delivery.",
    }


def tool_mechanical_wren_session(args: dict[str, Any]) -> dict[str, Any]:
    """Persist a bounded two-way hardware and product-story planning exchange."""
    session_id = require_string(args, "session_id", max_length=80)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
        raise NawnieError("session_id may contain only letters, numbers, hyphens, and underscores")
    speaker = require_string(args, "speaker", max_length=40)
    if speaker not in {"mechanical-engineer", "wren"}:
        raise NawnieError("speaker must be mechanical-engineer or wren")
    message = require_string(args, "message", max_length=4000)
    constraints = args.get("constraints", [])
    if not isinstance(constraints, list) or not all(isinstance(item, str) and item.strip() for item in constraints):
        raise NawnieError("constraints must be an array of non-empty strings")
    MECHANICAL_WREN_SESSION_HOME.mkdir(parents=True, exist_ok=True)
    destination = MECHANICAL_WREN_SESSION_HOME / f"{session_id}.jsonl"
    entry = {
        "schema": "shawn-core.mechanical-wren-session.v1",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "speaker": speaker,
        "message": message,
        "constraints": constraints,
        "handoff": "The other specialist responds to the open design question. Nawnie validates the combined plan and routes only missing proof.",
    }
    with destination.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(entry, sort_keys=True) + "\n")
    history = [json.loads(line) for line in destination.read_text(encoding="utf-8").splitlines()[-24:]]
    return {
        "schema": "shawn-core.mechanical-wren-session.v1",
        "specialists": ["mechanical-engineer", "wren"],
        "session_id": session_id,
        "path": str(destination),
        "entry": entry,
        "history": history,
        "acceptance": "This records collaborative planning. It is not a mechanical safety, thermal, electrical, manufacturing, or regulatory certification.",
    }


def tool_game_dev_capabilities(_args: dict[str, Any]) -> dict[str, Any]:
    """Describe Pixel's owned game-engine/toolchain lanes without overstating runtime health."""
    inventory = tool_core_tool_inventory({"specialist_id": "game-dev"})
    return {
        "schema": "shawn-core.game-dev-capabilities.v1",
        "specialist": "game-dev",
        "display_name": "Pixel",
        "owns": [
            "engine and toolchain readiness checks (Unreal, Unity, Godot installs and versions)",
            "per-project game-engine MCP wiring guidance (plugin flags, auto-start config, .mcp.json)",
            "project scaffolding conventions and asset-pipeline handoff between CAD/Blender and an engine",
        ],
        "does_not_own": [
            "live Unreal-editor mutation once a project's own unreal-mcp bridge is wired; that connection's tools are used directly, not re-implemented here",
            "Bethesda Creation Kit modding of an existing shipped game (Starfield/Skyrim/Fallout), owned by Creation Kit/Carl",
            "3D asset modeling, drawings, and CAD export verification, owned by CAD",
            "cross-domain routing, conflict resolution, or final synthesis, owned by Nawnie",
            "independent acceptance, owned by Verifier",
        ],
        "collaboration": {
            "cad": "Owns manufacturable/technical 3D modeling; Pixel owns getting that geometry into an engine project.",
            "creation-kit": "Owns modding an already-shipped Bethesda game; Pixel owns building an original engine project instead.",
            "wren": "Owns browser-facing UX; Pixel owns the engine-side project when a game (not a web app) is the deliverable.",
            "verifier": "Independently checks acceptance criteria, regression risk, and receipts.",
            "nawnie": "Decomposes the whole task, resolves overlap, and synthesizes delivery.",
        },
        "engine_bridges": {
            "unreal": "The unreal-mcp Agent Skill talks to a live Unreal Editor over a per-project MCP server. It only activates once a project's .uproject enables the ModelContextProtocol and AllToolsets plugins and the editor is running; see that skill's references/setup.md for first-time wiring. Pixel does not duplicate its live-editor tools.",
            "unity": "No installed bridge on this machine yet. Unity Editor and an MCP bridge (for example a community Unity MCP package) would need to be installed first.",
            "godot": "No installed bridge on this machine yet. Godot itself is not installed.",
        },
        "inventory": inventory,
        "boundary": "Registered paths are callable/present on disk. They do not prove an engine editor is running, a project's MCP plugin is enabled, or any asset actually imports cleanly until task-specific checks run.",
    }


def tool_game_dev_route(args: dict[str, Any]) -> dict[str, Any]:
    """Classify a game-dev task into Pixel-owned lanes and required handoff gates."""
    task = require_string(args, "task")
    lowered = task.casefold()
    lanes: list[str] = []
    if any(term in lowered for term in ("install unreal", "install unity", "install godot", "which engine", "engine version", "is unreal installed", "do i have unreal")):
        lanes.append("engine-setup")
    if any(term in lowered for term in (".uproject", "enable plugin", "mcp.json", "alltoolsets", "modelcontextprotocol", "wire up", "new project", "create a project", "scaffold")):
        lanes.append("project-wiring")
    if any(term in lowered for term in ("import mesh", "export to unreal", "export to unity", "blender to", "cad to", "asset pipeline", "texture pipeline", "bring the model into")):
        lanes.append("asset-pipeline")
    gameplay_signal = any(term in lowered for term in ("blueprint logic", "gameplay", "level design", "game mechanic", "player controller", "ai behavior tree", "combat system", "quest system"))
    lanes = list(dict.fromkeys(lanes))
    return {
        "schema": "shawn-core.game-dev-route.v1",
        "specialist": "game-dev",
        "task": task,
        "owned": bool(lanes),
        "lanes": lanes,
        "required_handoffs": {
            "cad": any(term in lowered for term in ("manufacturable", "3d print", "assembly drawing", "step export", "stl export")),
            "creation-kit": any(term in lowered for term in ("starfield", "skyrim", "fallout", "bethesda", "existing mod", "existing save")),
            "gameplay_implementation_not_owned_by_pixel": gameplay_signal,
            "verifier": True,
        },
        "proof_gates": [
            "registered engine/tool paths are not proof a project is open or a bridge is responding",
            "confirm the target project's .uproject plugin flags before claiming the MCP bridge is wired",
            "confirm an asset actually imports before treating a pipeline handoff as complete",
            "return implementation and receipts to Verifier and Shawn Core validation",
        ],
        "boundary": "If no lane is selected, this is likely gameplay code, art direction, or general engineering rather than a Pixel-owned engine/wiring/pipeline task. Route it through Nawnie to the actual implementation owner instead of assigning it to Pixel.",
    }


def tool_route(args: dict[str, Any]) -> dict[str, Any]:
    return route(require_string(args, "task"))


def tool_recommend_commands(args: dict[str, Any]) -> dict[str, Any]:
    task = require_string(args, "task")
    return {"task": task, "recommended_commands": command_recommendations(task)}


def tool_manifest_status(args: dict[str, Any]) -> dict[str, Any]:
    scope = args.get("scope", "all")
    if scope not in {"all", "model", "research"}:
        raise NawnieError("scope must be all, model, or research")
    query = args.get("query")
    if query is not None and (not isinstance(query, str) or len(query) > 200):
        raise NawnieError("query must be a string of at most 200 characters")
    result: dict[str, Any] = {"scope": scope}
    if scope in {"all", "model"}:
        result["model_inventory"] = model_manifest_status()
    if scope in {"all", "research"}:
        result["research_manifests"] = research_manifest_status(query)
    return result


def tool_carl_enter_workshop(_args: dict[str, Any]) -> dict[str, Any]:
    skill = {"present": CREATION_KIT_SKILL.is_file(), "path": str(CREATION_KIT_SKILL)}
    if CREATION_KIT_SKILL.is_file():
        skill.update({**file_receipt(CREATION_KIT_SKILL), "sha256": sha256_file(CREATION_KIT_SKILL)})
    return {
        "schema": "carl.workshop.v1",
        "specialist": "creation-kit",
        "role": SPECIALISTS["creation-kit"]["role"],
        "skill_owner": "creation-kit",
        "skill_receipt": skill,
        "supported_games": sorted(GAME_EXTENDERS),
        "lanes": {key: {item: value for item, value in lane.items() if item != "signals"} for key, lane in CARL_LANES.items()},
        "tools": ["carl_enter_workshop", "carl_route", "carl_plugin_audit", "carl_creation_kit_audit", "carl_package_audit"],
        "failure_shield": "For new crashes, inspect newest deployed changes and validate every declared master before runtime logs, load-order folklore, or broad mod disabling.",
        "boundary": "Carl is read-only inside Shawn Core. Manager mutations remain explicit host actions with post-deploy verification.",
    }


def tool_carl_route(args: dict[str, Any]) -> dict[str, Any]:
    task = require_string(args, "task")
    lowered = task.casefold()
    matches = [
        {"lane": name, "owner": lane["owner"], "gate": lane["gate"]}
        for name, lane in CARL_LANES.items()
        if any(signal in lowered for signal in lane["signals"])
    ]
    if not matches:
        matches = [{"lane": "conflict", "owner": CARL_LANES["conflict"]["owner"], "gate": CARL_LANES["conflict"]["gate"]}]
    return {
        "schema": "carl.route.v1",
        "specialist": "creation-kit",
        "task": task,
        "lanes": matches,
        "first_check": "carl_creation_kit_audit" if any(item["lane"] == "crash" for item in matches) else "carl_plugin_audit",
        "evidence_order": ["exact live game/profile state", "newest relevant file changes", "plugin headers and masters", "manager ownership", "runtime logs", "community hypotheses"],
    }


def tool_carl_plugin_audit(args: dict[str, Any]) -> dict[str, Any]:
    plugin = require_local_path(args, "plugin_path", directory=False)
    data_path = require_local_path(args, "data_path", directory=True)
    if plugin.suffix.casefold() not in {".esm", ".esp", ".esl"}:
        raise NawnieError("plugin_path must end in .esm, .esp, or .esl")
    plugins_path_raw = args.get("plugins_path")
    active: set[str] = set()
    inspected = [{"path": str(plugin), "purpose": "TES4 plugin audit"}, {"path": str(data_path), "purpose": "master presence root"}]
    if plugins_path_raw is not None:
        if not isinstance(plugins_path_raw, str):
            raise NawnieError("plugins_path must be an absolute file path")
        plugins_path = Path(plugins_path_raw).expanduser()
        if not plugins_path.is_absolute() or not plugins_path.is_file():
            raise NawnieError("plugins_path must be an existing absolute file path")
        plugins_path = plugins_path.resolve(strict=True)
        active, _ = active_plugins(plugins_path)
        inspected.append({"path": str(plugins_path), "purpose": "active plugin list"})
    masters = plugin_masters(plugin)
    missing = [master for master in masters if not (data_path / master).is_file()]
    core_masters = set().union(*(item[1] for item in GAME_EXTENDERS.values()))
    inactive = [master for master in masters if active and master.casefold() not in core_masters and master.casefold() not in active and (data_path / master).is_file()]
    return {
        "schema": "carl.plugin-audit.v1",
        "specialist": "creation-kit",
        "plugin": {**file_receipt(plugin), "sha256": sha256_file(plugin), "masters": masters},
        "missing_masters": missing,
        "inactive_masters": inactive,
        "inspected_paths": inspected,
        "checks": [
            {"check": "tes4_header", "status": "passed"},
            {"check": "master_presence", "status": "failed" if missing else "passed"},
            {"check": "master_activation", "status": "failed" if inactive else "passed", "applicable": bool(active)},
        ],
        "boundary": "Read-only. This does not clean, compact, re-master, deploy, or enable the plugin.",
    }


def tool_carl_package_audit(args: dict[str, Any]) -> dict[str, Any]:
    package_path = require_local_path(args, "package_path", directory=True)
    max_files = args.get("max_files", 2_000)
    if not isinstance(max_files, int) or isinstance(max_files, bool) or not 1 <= max_files <= 5_000:
        raise NawnieError("max_files must be an integer from 1 to 5000")
    categories = {"plugins": [], "archives": [], "papyrus": [], "meshes": [], "textures": [], "materials": [], "other": []}
    extension_map = {
        ".esm": "plugins", ".esp": "plugins", ".esl": "plugins",
        ".ba2": "archives", ".bsa": "archives", ".pex": "papyrus", ".psc": "papyrus",
        ".nif": "meshes", ".dds": "textures", ".mat": "materials",
    }
    files: list[Path] = []
    for directory, dirnames, filenames in os.walk(package_path, followlinks=False):
        dirnames[:] = [name for name in dirnames if not (Path(directory) / name).is_symlink()]
        for filename in filenames:
            files.append(Path(directory) / filename)
            if len(files) > max_files:
                raise NawnieError(f"package exceeds max_files={max_files}")
    for path in sorted(files, key=lambda item: str(item).casefold()):
        category = extension_map.get(path.suffix.casefold(), "other")
        receipt = {**file_receipt(path), "relative_path": str(path.relative_to(package_path)), "sha256": sha256_file(path) if path.stat().st_size <= 256 * 1024 * 1024 else None}
        categories[category].append(receipt)
    plugin_audits = []
    for item in categories["plugins"]:
        plugin_path = Path(item["path"])
        try:
            plugin_audits.append({"path": str(plugin_path), "masters": plugin_masters(plugin_path), "parse_error": None})
        except NawnieError as exc:
            plugin_audits.append({"path": str(plugin_path), "masters": [], "parse_error": str(exc)})
    return {
        "schema": "carl.package-audit.v1",
        "specialist": "creation-kit",
        "package": {"path": str(package_path), "file_count": len(files)},
        "categories": categories,
        "plugin_audits": plugin_audits,
        "checks": [
            {"check": "bounded_inventory", "status": "passed", "detail": f"Inventoried {len(files)} files without following directory symlinks."},
            {"check": "plugin_headers", "status": "failed" if any(item["parse_error"] for item in plugin_audits) else "passed", "detail": f"Parsed {len(plugin_audits)} plugin headers."},
            {"check": "archive_and_loose_assets_separated", "status": "passed"},
        ],
        "boundary": "Read-only package inspection. Runtime behavior, winning records, archive contents, and in-game rendering remain separate gates.",
    }


def tool_carl_creation_kit_audit(args: dict[str, Any]) -> dict[str, Any]:
    game = args.get("game")
    if game not in GAME_EXTENDERS:
        raise NawnieError("game must be starfield, skyrimse, or fallout4")
    data_path = require_local_path(args, "data_path", directory=True)
    plugins_path = require_local_path(args, "plugins_path", directory=False)
    recent_hours = args.get("recent_hours", 72)
    recent_limit = args.get("recent_limit", 100)
    if not isinstance(recent_hours, int) or isinstance(recent_hours, bool) or not 1 <= recent_hours <= 720:
        raise NawnieError("recent_hours must be an integer from 1 to 720")
    if not isinstance(recent_limit, int) or isinstance(recent_limit, bool) or not 1 <= recent_limit <= 500:
        raise NawnieError("recent_limit must be an integer from 1 to 500")

    extender, core_masters = GAME_EXTENDERS[game]
    active, plugin_lines = active_plugins(plugins_path)
    cutoff = time.time() - recent_hours * 3600
    plugin_files = sorted(
        (path for path in data_path.iterdir() if path.is_file() and path.suffix.casefold() in {".esm", ".esp", ".esl"}),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    recent = [path for path in plugin_files if path.stat().st_mtime >= cutoff][:recent_limit]
    if not recent:
        recent = plugin_files[:recent_limit]

    inspected_paths: list[dict[str, Any]] = [
        {**file_receipt(plugins_path), "purpose": "authoritative active plugin list"},
        {"path": str(data_path), "purpose": "authoritative deployed game Data directory"},
    ]
    checks: list[dict[str, Any]] = [
        {"check": "recent_changes_first", "status": "passed", "detail": f"Inspected {len(recent)} newest plugin files before runtime logs."},
        {"check": "active_plugin_snapshot", "status": "passed", "detail": f"Read {len(plugin_lines)} lines and {len(active)} active entries from Plugins.txt."},
    ]
    recent_changes: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    for plugin in recent:
        try:
            masters = plugin_masters(plugin)
            parse_error = None
        except NawnieError as exc:
            masters = []
            parse_error = str(exc)
        missing = [master for master in masters if not (data_path / master).is_file()]
        inactive = [
            master for master in masters
            if master.casefold() not in core_masters and master.casefold() not in active and (data_path / master).is_file()
        ]
        receipt = {
            **file_receipt(plugin),
            "sha256": sha256_file(plugin),
            "active": plugin.name.casefold() in active,
            "masters": masters,
            "missing_masters": missing,
            "inactive_masters": inactive,
            "parse_error": parse_error,
        }
        recent_changes.append(receipt)
        inspected_paths.append({"path": str(plugin), "purpose": "recent plugin TES4 master audit"})
        if receipt["active"] and (missing or inactive or parse_error):
            blockers.append({
                "severity": "high",
                "plugin": plugin.name,
                "reason": "parse_error" if parse_error else "missing_or_inactive_master",
                "missing_masters": missing,
                "inactive_masters": inactive,
                "parse_error": parse_error,
            })

    native_dir = data_path / extender / "Plugins"
    native_plugins = []
    if native_dir.is_dir():
        inspected_paths.append({"path": str(native_dir), "purpose": f"native {extender} DLL inventory"})
        for dll in sorted(native_dir.glob("*.dll"), key=lambda path: path.name.casefold()):
            native_plugins.append({**file_receipt(dll), "sha256": sha256_file(dll), "classification": f"native_{extender.casefold()}_dll"})

    scripts_dir = data_path / "Scripts"
    recent_scripts = []
    if scripts_dir.is_dir():
        inspected_paths.append({"path": str(scripts_dir), "purpose": "Papyrus PEX inventory"})
        for pex in sorted(scripts_dir.rglob("*.pex"), key=lambda path: path.stat().st_mtime, reverse=True):
            if pex.stat().st_mtime < cutoff:
                continue
            recent_scripts.append({**file_receipt(pex), "sha256": sha256_file(pex), "classification": "papyrus_pex_not_native_extender"})
            if len(recent_scripts) >= recent_limit:
                break

    checks.extend([
        {"check": "declared_master_gate", "status": "failed" if blockers else "passed", "detail": f"Found {len(blockers)} active recent plugins with dependency or parse blockers."},
        {"check": "native_extender_separation", "status": "passed", "detail": f"Inventoried {len(native_plugins)} {extender} DLLs separately from ESP/ESM/ESL and Papyrus."},
        {"check": "papyrus_separation", "status": "passed", "detail": f"Inventoried {len(recent_scripts)} recent PEX scripts; PEX does not imply {extender}."},
    ])
    return {
        "schema": "carl.creation-kit-audit.v1",
        "specialist": "creation-kit",
        "skill_owner": "creation-kit",
        "game": game,
        "inspection_order": ["recent deployed plugin changes", "TES4 declared masters", "active Plugins.txt", f"native {extender} DLLs", "Papyrus PEX scripts", "runtime evidence only after structural gates"],
        "inspected_paths": inspected_paths,
        "checks": checks,
        "recent_changes": recent_changes,
        "priority_findings": blockers,
        "native_extender_plugins": native_plugins,
        "recent_papyrus_scripts": recent_scripts,
        "boundary": "Read-only structural diagnosis. No plugin, load order, manager state, game file, or save was changed.",
    }


def tool_call_agent(args: dict[str, Any]) -> dict[str, Any]:
    task = require_string(args, "task")
    requested = require_string(args, "agent", max_length=80).casefold().lstrip("@")
    aliases = {alias.casefold().lstrip("@"): specialist_id for specialist_id, spec in SPECIALISTS.items() for alias in spec["aliases"]}
    if requested not in aliases:
        raise NawnieError(f"agent must be one of: {', '.join(sorted(SPECIALISTS))}")
    agent = aliases[requested]
    spec = SPECIALISTS[agent]
    available = agent not in CHILD_MCP_ROUTES or (CHILD_MCP_ROUTES[agent][0].is_dir() and (CHILD_MCP_ROUTES[agent][0] / CHILD_MCP_ROUTES[agent][1][0]).is_file())
    return {
        "agent": agent,
        "available": available,
        "task": task,
        "handoff": {
            "mcp_server": "shawn-core",
            "first_tool": "shawn_core_specialist_execute",
            "specialist_tool": spec["first_tool"],
            "instruction": "Use shawn_core_specialist_tools to inspect the specialist's executable surface, then invoke its owned tool through shawn_core_specialist_execute and return the evidence to Shawn Core validation.",
        },
        "route": route(task)["route"],
    }


def tool_spawn_agent(args: dict[str, Any]) -> dict[str, Any]:
    command, cwd, timeout, plan = build_agent_command(args)
    if args.get("execute", False) is not True:
        return {"status": "planned", "plan": plan, "next_step": "Set execute=true to start this read-only local Codex subagent."}
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="nawnie-child-") as temporary_dir:
        final_message_path = Path(temporary_dir) / "final-message.txt"
        command, cwd, timeout, plan = build_agent_command(args, final_message_path)
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
                **hidden_subprocess_kwargs(),
            )
        except subprocess.TimeoutExpired as exc:
            diagnostic = (exc.stderr or "")[-MAX_FAILURE_DIAGNOSTIC_CHARS:]
            return {
                "status": "timed_out",
                "plan": plan,
                "timeout_seconds": timeout,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "diagnostic": diagnostic or None,
                "result_capture": "No child transcript was returned.",
            }
        try:
            final_message = final_message_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            final_message = ""
    truncated = len(final_message) > MAX_FINAL_MESSAGE_CHARS
    result: dict[str, Any] = {
        "status": "completed" if completed.returncode == 0 else "failed",
        "plan": plan,
        "exit_code": completed.returncode,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "final_message": final_message[:MAX_FINAL_MESSAGE_CHARS],
        "final_message_truncated": truncated,
        "result_capture": "Child process was awaited. Only its final message is returned; event output was discarded.",
    }
    if completed.returncode != 0:
        result["diagnostic"] = completed.stderr[-MAX_FAILURE_DIAGNOSTIC_CHARS:] or None
    return result


def tool_compare(args: dict[str, Any]) -> dict[str, Any]:
    prompt = require_string(args, "prompt")
    left_model = args.get("left_model", "gpt-5.6-sol")
    right_model = args.get("right_model", "gpt-5.6-sol-wm")
    reasoning = args.get("reasoning_effort", "high")
    execute = args.get("execute", False) is True
    common = {"prompt": prompt, "reasoning_effort": reasoning, "execute": execute, "cwd": args.get("cwd", str(PLUGIN_ROOT)), "timeout_seconds": args.get("timeout_seconds", 120)}
    left = tool_spawn_agent({**common, "model": left_model})
    right = tool_spawn_agent({**common, "model": right_model})
    return {"comparison": "bounded local Codex subagent comparison", "left": left, "right": right}


def _parse_object_json(value: Any, *, label: str) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, str) or len(value) > MAX_PROMPT_CHARS:
        raise NawnieError(f"{label} must be a JSON object string within the size limit")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise NawnieError(f"{label} must be valid JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise NawnieError(f"{label} must decode to an object")
    return parsed


def _child_mcp_exchange(specialist: str, method: str, params: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    """Run one bounded request against an allowlisted local child MCP server."""
    route = CHILD_MCP_ROUTES.get(specialist)
    if route is None:
        raise NawnieError(f"{specialist} is gateway-native, not a child MCP route")
    cwd, entry = route
    if not cwd.is_dir() or not (cwd / entry[0]).is_file():
        raise NawnieError(f"Installed {specialist} runtime is unavailable at the registered path")
    executable = (cwd / ".venv/Scripts/python.exe") if specialist == "cad" else Path(sys.executable)
    if not executable.is_file():
        raise NawnieError(f"Registered {specialist} Python runtime is unavailable")
    command = [str(executable), "-B", *entry]
    if specialist == "cad":
        return _cad_mcp_exchange(command, cwd, method, params, timeout_seconds)
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "shawn-core", "version": SERVER_VERSION}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": method, "params": params},
    ]
    try:
        completed = subprocess.run(
            command, cwd=cwd, input="\n".join(json.dumps(item) for item in requests) + "\n",
            text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout_seconds, check=False,
            **hidden_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        raise NawnieError(f"{specialist} MCP request timed out after {timeout_seconds} seconds") from exc
    replies: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            replies.append(parsed)
    reply = next((item for item in replies if item.get("id") == 2), None)
    if reply is None:
        diagnostic = completed.stderr[-MAX_FAILURE_DIAGNOSTIC_CHARS:].strip()
        raise NawnieError(f"{specialist} MCP returned no reply" + (f": {diagnostic}" if diagnostic else ""))
    if "error" in reply:
        raise NawnieError(f"{specialist} MCP error: {reply['error']}")
    result = reply.get("result")
    if not isinstance(result, dict):
        raise NawnieError(f"{specialist} MCP returned a malformed result")
    return result


def _cad_mcp_exchange(command: list[str], cwd: Path, method: str, params: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    """CAD's MCP runtime requires initialize to be acknowledged before a tool call."""
    process = subprocess.Popen(command, cwd=cwd, text=True, encoding="utf-8", errors="replace", stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **hidden_subprocess_kwargs())
    assert process.stdin and process.stdout

    def read_reply(expected_id: int) -> dict[str, Any]:
        box: list[str] = []
        reader = threading.Thread(target=lambda: box.append(process.stdout.readline()), daemon=True)
        reader.start()
        reader.join(timeout_seconds)
        if reader.is_alive() or not box or not box[0].strip():
            raise NawnieError(f"cad MCP request timed out after {timeout_seconds} seconds")
        try:
            reply = json.loads(box[0])
        except json.JSONDecodeError as exc:
            raise NawnieError("cad MCP returned malformed JSON") from exc
        if not isinstance(reply, dict) or reply.get("id") != expected_id:
            raise NawnieError("cad MCP returned an unexpected reply")
        if "error" in reply:
            raise NawnieError(f"cad MCP error: {reply['error']}")
        result = reply.get("result")
        if not isinstance(result, dict):
            raise NawnieError("cad MCP returned a malformed result")
        return result

    try:
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "shawn-core", "version": SERVER_VERSION}}}) + "\n")
        process.stdin.flush()
        read_reply(1)
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n")
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": method, "params": params}) + "\n")
        process.stdin.flush()
        return read_reply(2)
    finally:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


def _native_specialist_tools(specialist: str) -> list[dict[str, Any]]:
    native: dict[str, tuple[str, ...]] = {
        "nawnie": ("shawn_core_context", "shawn_core_query", "shawn_core_validate", "nawnie_route"),
        "al": ("al_capabilities", "al_route", "nawnie_manifest_status", "nawnie_recommend_commands"),
        "operator": ("operator_capabilities", "operator_file", "operator_terminal"),
        "mechanical-engineer": ("mechanical_wren_session",),
        "game-dev": ("game_dev_capabilities", "game_dev_route"),
        "token-master": ("token_master_audit",),
        "chrono": ("chrono_plan",),
        "changelog": ("changelog_intake", "changelog_finalize"),
    }
    names = native.get(specialist)
    if names is None:
        raise NawnieError(f"{specialist} has no gateway-native tools")
    return [{key: value for key, value in TOOLS[name].items() if key != "handler"} | {"name": name} for name in names]


# These five roles share a child server, but not ownership of one another's tools.
SHARED_ROLE_PREFIXES = {
    "corporate-overview": "corporate_overview_", "sensai-advisor": "sensai_advisor_",
    "verifier": "verifier_", "researcher": "researcher_", "legal-readiness": "legal_readiness_",
}


def tool_specialist_tools(args: dict[str, Any]) -> dict[str, Any]:
    specialist = canonical_specialist_id(require_string(args, "specialist", max_length=80))
    if specialist not in SPECIALISTS:
        raise NawnieError("unknown specialist")
    detail = _response_detail(args)
    query = args.get("query", "")
    limit = args.get("limit", 1000)
    if not isinstance(query, str) or len(query) > 200:
        raise NawnieError("query must be a string of at most 200 characters")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise NawnieError("limit must be an integer between 1 and 1000")
    if specialist in CHILD_MCP_ROUTES:
        result = _child_mcp_exchange(specialist, "tools/list", {}, CHILD_MCP_TIMEOUT_SECONDS)
        tools = result.get("tools")
        if not isinstance(tools, list) or not tools:
            raise NawnieError(f"{specialist} MCP did not expose tools")
        if any(not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in tools):
            raise NawnieError(f"{specialist} MCP returned malformed tool metadata")
        prefix = SHARED_ROLE_PREFIXES.get(specialist)
        if prefix:
            tools = [item for item in tools if item["name"].startswith(prefix)]
        if not tools:
            raise NawnieError(f"{specialist} MCP did not expose its owned tools")
        transport = "shawn-core-child-mcp"
    else:
        tools = _native_specialist_tools(specialist)
        transport = "shawn-core-native"
    total = len(tools)
    terms = query.casefold().split()
    matched = [item for item in tools if all(term in (item["name"] + " " + str(item.get("description", ""))).casefold() for term in terms)]
    selected = matched[:limit]
    if detail == "summary":
        selected = [{"name": item["name"], "description": str(item.get("description", ""))[:240], "annotations": item.get("annotations", {})} for item in selected]
    return {"specialist": specialist, "transport": transport, "tools": selected,
            "tool_count": len(selected), "total_tool_count": total, "matched_tool_count": len(matched),
            "truncated": len(matched) > limit, "detail": detail,
            "availability": "listed-now; execution, authentication and task-specific readiness are unproven"}


def tool_specialist_execute(args: dict[str, Any]) -> dict[str, Any]:
    specialist = canonical_specialist_id(require_string(args, "specialist", max_length=80))
    tool_name = canonical_tool_name(require_string(args, "tool", max_length=160))
    if specialist not in SPECIALISTS:
        raise NawnieError("unknown specialist")
    arguments = _parse_object_json(args.get("arguments_json", "{}"), label="arguments_json")
    timeout = args.get("timeout_seconds", CHILD_MCP_TIMEOUT_SECONDS)
    if not isinstance(timeout, int) or timeout < 1 or timeout > MAX_TIMEOUT_SECONDS:
        raise NawnieError(f"timeout_seconds must be an integer between 1 and {MAX_TIMEOUT_SECONDS}")
    prefix = SHARED_ROLE_PREFIXES.get(specialist)
    if prefix and not tool_name.startswith(prefix):
        raise NawnieError(f"{tool_name} is not owned by specialist {specialist}; select its owner explicitly")
    if specialist in CHILD_MCP_ROUTES:
        result = _child_mcp_exchange(specialist, "tools/call", {"name": tool_name, "arguments": arguments}, timeout)
        if result.get("isError") is True:
            # Keep the specialist's bounded reason so the host can change its hypothesis.
            blocks = result.get("content", [])
            diagnostic = " ".join(str(item.get("text", "")) for item in blocks if isinstance(item, dict) and item.get("type") == "text")[:MAX_FAILURE_DIAGNOSTIC_CHARS]
            raise NawnieError(f"{specialist}.{tool_name} reported an error" + (f": {diagnostic}" if diagnostic else ""))
        return {"specialist": specialist, "tool": tool_name, "transport": "shawn-core-child-mcp", "result": result}
    allowed = {item["name"] for item in _native_specialist_tools(specialist)}
    if tool_name not in allowed:
        raise NawnieError(f"{tool_name} is not owned by gateway-native specialist {specialist}")
    payload = TOOLS[tool_name]["handler"](arguments)
    return {"specialist": specialist, "tool": tool_name, "transport": "shawn-core-native", "result": payload}



def tool_diagnose(args: dict[str, Any]) -> dict[str, Any]:
    try:
        return diagnose(require_string(args, "symptom", max_length=4000), args.get("evidence", []))
    except ValueError as exc:
        raise NawnieError(str(exc)) from exc


def tool_tool_help(args: dict[str, Any]) -> dict[str, Any]:
    name = require_string(args, "tool_name", max_length=160)
    arguments = _parse_object_json(args.get("arguments_json", "{}"), label="arguments_json")
    checked = preflight_tool_call(name, arguments, TOOLS)
    return {
        "schema": "shawn-core.tool-help.v1",
        "requested_tool": name,
        "valid": checked["valid"],
        "errors": checked["errors"],
        "suggestions": checked["suggestions"],
        "advertised_input_schema": checked.get("schema"),
        "boundary": "Read-only preflight. Does not execute tools or silently rewrite caller arguments.",
    }


def tool_specialist_agent(args: dict[str, Any]) -> dict[str, Any]:
    """Give a named specialist its own isolated read-only reasoning invocation."""
    agent = canonical_specialist_id(require_string(args, "agent", max_length=80))
    if agent not in SPECIALISTS:
        raise NawnieError(f"unknown specialist agent: {agent}; choose from: {', '.join(sorted(SPECIALISTS))}")
    task = require_string(args, "task", max_length=8000)
    execute = args.get("execute", False)
    if type(execute) is not bool:
        raise NawnieError("execute must be a boolean")
    try:
        profile = agent_profile(agent, SPECIALISTS[agent]["role"])
        prompt = agent_prompt(agent, SPECIALISTS[agent]["role"], task, args.get("evidence", []))
    except ValueError as exc:
        raise NawnieError(str(exc)) from exc
    invoke = {
        "prompt": prompt,
        "model": args.get("model", "gpt-5.6-sol"),
        "reasoning_effort": args.get("reasoning_effort", "medium"),
        "cwd": args.get("cwd", str(PLUGIN_ROOT)),
        "timeout_seconds": args.get("timeout_seconds", 120),
        "execute": execute,
    }
    output = tool_spawn_agent(invoke)
    return {
        "schema": "shawn-core.specialist-agent.v1",
        "agent": agent,
        "profile": profile,
        "execution": output,
        "boundary": "Separate read-only Codex invocation only when execute=true; no persistent agent memory, write access or deployed runtime is implied.",
        "handoff": "Return receipts to Nawnie; acceptance belongs to Verifier.",
    }



def _repair_actor(actor: str, *, implementer: bool = False) -> None:
    if actor not in SPECIALISTS or (implementer and actor == "verifier"):
        raise NawnieError("unknown or ineligible repair specialist: " + str(actor))


def tool_repair_open(args: dict[str, Any]) -> dict[str, Any]:
    _repair_actor(args["owner"], implementer=True)
    try:
        result = RepairLedger().open(**args)
    except ValueError as exc:
        raise NawnieError(str(exc)) from exc
    return {"schema": "shawn-core.repair-case.v1", "record": result,
            "boundary": "State persistence only, not agent execution or authorization."}


def tool_repair_note(args: dict[str, Any]) -> dict[str, Any]:
    _repair_actor(args["actor"])
    try:
        result = RepairLedger().note(**args)
    except ValueError as exc:
        raise NawnieError(str(exc)) from exc
    return {"schema": "shawn-core.repair-event.v1", "record": result}


def tool_repair_transition(args: dict[str, Any]) -> dict[str, Any]:
    _repair_actor(args["actor"])
    if args.get("new_owner"):
        _repair_actor(args["new_owner"], implementer=True)
    try:
        result = RepairLedger().transition(**args)
    except ValueError as exc:
        raise NawnieError(str(exc)) from exc
    return {"schema": "shawn-core.repair-transition.v1", "record": result,
            "boundary": "Actor names are caller claims, not authenticated identities. A report cannot grant tool permissions or certify a live deployment."}


def tool_repair_get(args: dict[str, Any]) -> dict[str, Any]:
    try:
        return {"schema": "shawn-core.repair-case.v1", **RepairLedger().get(args["case_id"])}
    except ValueError as exc:
        raise NawnieError(str(exc)) from exc


def tool_repair_list(args: dict[str, Any]) -> dict[str, Any]:
    try:
        cases = RepairLedger().list(args.get("limit", 30))
    except ValueError as exc:
        raise NawnieError(str(exc)) from exc
    return {"schema": "shawn-core.repair-cases.v1", "cases": cases}


ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]


def tool(description: str, handler: ToolHandler, input_schema: dict[str, Any], *, read_only: bool) -> dict[str, Any]:
    return {
        "description": description,
        "handler": handler,
        "inputSchema": input_schema,
        "annotations": {"readOnlyHint": read_only, "destructiveHint": False, "idempotentHint": read_only, "openWorldHint": False},
    }


TOOLS: dict[str, dict[str, Any]] = {
    "shawn_core_context": tool("Return the bounded Shawn Core specialist registry, Nawnie ownership model, AES fallback, and legacy naming policy.", tool_core_context, schema({}), read_only=True),
    "shawn_core_diagnose": tool("Diagnose a failing LLM, MCP tool or agent: rank failure layers, suggest accountable owners and falsifiable probes; never assert a root cause without testing.", tool_diagnose, schema({"symptom": {"type": "string", "minLength": 1, "maxLength": 4000}, "evidence": {"type": "array", "items": {"type": "string", "maxLength": 2000}, "maxItems": 20}}, ["symptom"]), read_only=True),
    "shawn_core_tool_help": tool("Preflight an MCP tool name and JSON object against its advertised schema without executing or correcting arguments.", tool_tool_help, schema({"tool_name": {"type": "string", "minLength": 1, "maxLength": 160}, "arguments_json": {"type": "string", "maxLength": MAX_PROMPT_CHARS, "default": "{}"}}, ["tool_name"]), read_only=True),
    "shawn_core_specialist_agent": tool("Plan or explicitly run Wren, AL or another specialist as an isolated read-only Codex model invocation with role-specific contracts and proof gates. Default does not execute.", tool_specialist_agent, schema({"agent": {"type": "string", "enum": sorted(SPECIALISTS)}, "task": {"type": "string", "minLength": 1, "maxLength": 8000}, "evidence": {"type": "array", "items": {"type": "string", "maxLength": 2000}, "maxItems": 20}, "model": {"type": "string", "enum": sorted(ALLOWED_MODELS)}, "reasoning_effort": {"type": "string", "enum": sorted(ALLOWED_REASONING)}, "cwd": {"type": "string"}, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_TIMEOUT_SECONDS}, "execute": {"type": "boolean", "default": False}}, ["agent", "task"]), read_only=False),

    "shawn_core_repair_open": tool("Open or safely retry a durable specialist repair case with expected vs observed behavior and one implementation owner; no executor is started.", tool_repair_open, schema({"request_key": {"type": "string", "minLength": 1, "maxLength": 100}, "project": {"type": "string", "minLength": 1, "maxLength": 120}, "symptom": {"type": "string", "minLength": 1, "maxLength": 3000}, "expected": {"type": "string", "minLength": 1, "maxLength": 2000}, "actual": {"type": "string", "minLength": 1, "maxLength": 2000}, "owner": {"type": "string", "enum": sorted(set(SPECIALISTS) - {"verifier"})}}, ["request_key", "project", "symptom", "expected", "actual", "owner"]), read_only=False),
    "shawn_core_repair_note": tool("Append an immutable case observation, hypothesis, falsifying probe, change receipt, verifier report or teachable lesson. Requires the current revision and idempotency event ID.", tool_repair_note, schema({"case_id": {"type": "string", "minLength": 1, "maxLength": 80}, "event_id": {"type": "string", "minLength": 1, "maxLength": 120}, "actor": {"type": "string", "enum": sorted(SPECIALISTS)}, "kind": {"type": "string", "enum": ["observation", "hypothesis", "probe", "repair", "verification", "lesson"]}, "body": {"type": "string", "minLength": 1, "maxLength": 4000}, "expected_revision": {"type": "integer", "minimum": 0}, "source_ref": {"type": "string", "maxLength": 400}}, ["case_id", "event_id", "actor", "kind", "body", "expected_revision"]), read_only=False),
    "shawn_core_repair_transition": tool("Advance a repair case through evidence/receipt-gated states or explicitly hand off ownership. Claimed actors are not authenticated; no authorization or live success is implied.", tool_repair_transition, schema({"case_id": {"type": "string", "minLength": 1, "maxLength": 80}, "event_id": {"type": "string", "minLength": 1, "maxLength": 120}, "actor": {"type": "string", "enum": sorted(SPECIALISTS)}, "action": {"type": "string", "enum": ["start", "plan", "submit", "accept_report", "reject_report", "block", "resume", "handoff"]}, "expected_revision": {"type": "integer", "minimum": 0}, "reason": {"type": "string", "minLength": 1, "maxLength": 2000}, "new_owner": {"type": "string", "enum": sorted(set(SPECIALISTS) - {"verifier"})}}, ["case_id", "event_id", "actor", "action", "expected_revision", "reason"]), read_only=False),
    "shawn_core_repair_get": tool("Read a persisted repair case, append-only event history, and outstanding teaching/verification evidence gaps.", tool_repair_get, schema({"case_id": {"type": "string", "minLength": 1, "maxLength": 80}}, ["case_id"]), read_only=True),
    "shawn_core_repair_list": tool("List up to 100 recent repair cases from the local SQLite ledger; does not open agents.", tool_repair_list, schema({"limit": {"type": "integer", "minimum": 1, "maximum": 100}}), read_only=True),

    "shawn_core_query": tool("Create a Shawn Core request envelope, run Nawnie routing, select explicit MCP specialists or AES skills, and return the validation contract. Set mode=plan, or begin a legacy task with /plan, to run Chrono's bounded planning round.", tool_core_query, schema({"task": {"type": "string", "minLength": 1, "maxLength": MAX_PROMPT_CHARS}, "mode": {"type": "string", "enum": ["execute", "plan"], "default": "execute"}, "detail": {"type": "string", "enum": ["full", "compact"], "default": "full"}, "constraints": {"type": "array", "items": {"type": "string"}, "maxItems": 30}, "acceptance_criteria": {"type": "array", "items": {"type": "string"}, "maxItems": 30}}, ["task"]), read_only=True),
    "shawn_core_port_validate": tool("Validate a requested local TCP port against Shawn's fixed project reservations and live listeners. Return the preferred port when safe or the next free port in the caller's approved range without killing, relaunching, forwarding, or changing firewall state.", tool_core_port_validate, schema({"service_name": {"type": "string", "minLength": 1, "maxLength": 160}, "preferred_port": {"type": "integer", "minimum": 1024, "maximum": 65535}, "range_start": {"type": "integer", "minimum": 1024, "maximum": 65535}, "range_end": {"type": "integer", "minimum": 1024, "maximum": 65535}, "bind": {"type": "string", "enum": ["loopback", "wildcard"], "default": "loopback"}, "reservation_name": {"type": "string", "maxLength": 160}}, ["service_name", "preferred_port"]), read_only=True),
    "shawn_core_validate": tool("Validate a bounded specialist or AES result. Deterministic checks and receipts gate acceptance; failures return control to Nawnie for redistribution.", tool_core_validate, schema({"request_id": {"type": "string", "minLength": 1, "maxLength": 80}, "owner": {"type": "string", "minLength": 1, "maxLength": 80}, "status": {"type": "string", "enum": ["completed", "partial", "blocked", "failed"]}, "acceptance_met": {"type": "boolean"}, "deterministic_checks_passed": {"type": "boolean"}, "evidence": {"type": "array", "items": {"type": "string"}, "maxItems": 100}, "receipts": {"type": "array", "items": {"type": "string"}, "maxItems": 100}, "blockers": {"type": "array", "items": {"type": "string"}, "maxItems": 100}, "recommended_next_specialist": {"type": "string", "enum": sorted(SPECIALISTS)}}, ["request_id", "owner", "status", "acceptance_met", "deterministic_checks_passed"]), read_only=True),
    "shawn_core_tool_inventory": tool("Read the durable machine-local specialist tool registry and recheck every selected path without launching tools or scanning outside the registry.", tool_core_tool_inventory, schema({"specialist_id": {"type": "string", "maxLength": 80}}), read_only=True),
    "shawn_core_specialist_tools": tool("List the real executable tools for any registered Shawn Core specialist through this one MCP server. Plugin-backed roles are queried through fixed local child-MCP routes; gateway-native roles expose their owned Core tools.", tool_specialist_tools, schema({"specialist": {"type": "string", "enum": sorted(SPECIALISTS)}, "detail": {"type": "string", "enum": ["full", "summary"], "default": "full"}, "query": {"type": "string", "maxLength": 200}, "limit": {"type": "integer", "minimum": 1, "maximum": 1000}}, ["specialist"]), read_only=True),
    "shawn_core_specialist_execute": tool("Execute a named tool owned by any Shawn Core specialist through this one MCP server. The specialist, executable, working directory, and timeout are bounded; arguments_json must be a JSON object and no arbitrary command or path is accepted.", tool_specialist_execute, schema({"specialist": {"type": "string", "enum": sorted(SPECIALISTS)}, "tool": {"type": "string", "minLength": 1, "maxLength": 160}, "arguments_json": {"type": "string", "maxLength": MAX_PROMPT_CHARS, "default": "{}"}, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_TIMEOUT_SECONDS, "default": CHILD_MCP_TIMEOUT_SECONDS}}, ["specialist", "tool"]), read_only=False),
    "nawnie_status": tool("Inspect Nawnie's local router, installed specialists, and available bounded model routes.", tool_status, schema({}), read_only=True),
    "token_master_audit": tool("Have Token Master inspect active Codex token-relevant configuration, context/compaction facts, and resident MCP state. It is read-only and cannot change host limits.", tool_token_master_audit, schema({}), read_only=True),
    "chrono_plan": tool("Have Chrono own a bounded project-planning round with Nawnie and Token Master. It returns a dependency-ordered active plan plus contingent branches without starting agents, editing files, or changing host settings.", tool_chrono_plan, schema({"task": {"type": "string", "minLength": 1, "maxLength": MAX_PROMPT_CHARS}, "constraints": {"type": "array", "items": {"type": "string"}, "maxItems": 30}, "acceptance_criteria": {"type": "array", "items": {"type": "string"}, "maxItems": 30}}, ["task"]), read_only=True),
    "changelog_intake": tool("Start the Changelog specialist with its Luna execution contract and Atlas Cartographer, local Git, and @github core handoffs. execute=false returns a plan; execute=true runs the bounded read-only Luna startup intake.", tool_changelog_intake, schema({"task": {"type": "string", "minLength": 1, "maxLength": MAX_PROMPT_CHARS}, "cwd": {"type": "string"}, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_TIMEOUT_SECONDS}, "execute": {"type": "boolean", "default": False}}, ["task"]), read_only=False),
    "changelog_finalize": tool("Finalize the Changelog specialist with verified changes and receipts. This writes no file, makes no Git change, and returns the receipt-backed delivery handoff.", tool_changelog_finalize, schema({"title": {"type": "string", "minLength": 1, "maxLength": 300}, "changes": {"type": "array", "items": {"type": "string", "minLength": 1}, "maxItems": 100}, "receipts": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1, "maxItems": 100}, "blockers": {"type": "array", "items": {"type": "string", "minLength": 1}, "maxItems": 100}}, ["title", "receipts"]), read_only=True),
    "mechanical_wren_session": tool("Persist a bounded Mechanical Engineer and Wren AI-device design session. Each speaker can resume the same session_id; Nawnie retains synthesis and acceptance.", tool_mechanical_wren_session, schema({"session_id": {"type": "string", "minLength": 1, "maxLength": 80}, "speaker": {"type": "string", "enum": ["mechanical-engineer", "wren"]}, "message": {"type": "string", "minLength": 1, "maxLength": 4000}, "constraints": {"type": "array", "items": {"type": "string", "minLength": 1}, "maxItems": 30}}, ["session_id", "speaker", "message"]), read_only=False),
    "nawnie_recommend_commands": tool("Choose which Nawnie MCP commands to call for a task, including F: model or research-manifest checks when relevant.", tool_recommend_commands, schema({"task": {"type": "string", "minLength": 1, "maxLength": MAX_PROMPT_CHARS}}, ["task"]), read_only=True),
    "nawnie_manifest_status": tool("Read bounded metadata from the authoritative F: model inventory and F: research manifest roots. It does not load models or dataset contents.", tool_manifest_status, schema({"scope": {"type": "string", "enum": ["all", "model", "research"], "default": "all"}, "query": {"type": "string", "maxLength": 200}}), read_only=True),
    "al_capabilities": tool("Describe AL's AI-systems ownership, collaboration boundaries, proof gates, and registered local tool inventory without claiming runtime compatibility.", tool_al_capabilities, schema({}), read_only=True),
    "al_route": tool("Classify an AI engineering task across CUDA/GPU, LLM serving, training/evaluation, AI-specific FastAPI, data, and harness lanes, with Wren, Operator, and Verifier handoff gates.", tool_al_route, schema({"task": {"type": "string", "minLength": 1, "maxLength": MAX_PROMPT_CHARS}}, ["task"]), read_only=True),
    "operator_capabilities": tool("Describe the permission-aware local file, PowerShell, BAT, and bounded operational-scripting bridge for harnesses without native tools.", tool_operator_capabilities, schema({}), read_only=True),
    "game_dev_capabilities": tool("Describe Pixel's game-engine/toolchain ownership, collaboration boundaries, per-project MCP bridge status, and registered local tool inventory without claiming runtime compatibility.", tool_game_dev_capabilities, schema({}), read_only=True),
    "game_dev_route": tool("Classify a game-dev task across engine-setup, project-wiring, and asset-pipeline lanes, with CAD, Creation Kit, and gameplay-implementation handoff gates.", tool_game_dev_route, schema({"task": {"type": "string", "minLength": 1, "maxLength": MAX_PROMPT_CHARS}}, ["task"]), read_only=True),
    "operator_file": tool("Read text from an approved local root, or atomically write supplied UTF-8 text only with permission_grant=user-authorized-local. Deletes and recursive operations are unsupported.", tool_operator_file, schema({"action": {"type": "string", "enum": ["read", "write"], "default": "read"}, "path": {"type": "string", "minLength": 1, "maxLength": 1000}, "content": {"type": "string", "maxLength": 1000000}, "expected_sha256": {"type": "string", "maxLength": 64}, "permission_grant": {"type": "string", "enum": ["user-authorized-local"]}}, ["path"]), read_only=False),
    "operator_terminal": tool("Plan, or with permission_grant=user-authorized-local execute, a bounded PowerShell command in an approved local directory. Network, deletion, background, firewall, credential, and system-control commands are rejected.", tool_operator_terminal, schema({"command": {"type": "string", "minLength": 1, "maxLength": 8000}, "working_directory": {"type": "string", "minLength": 1, "maxLength": 1000}, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_TIMEOUT_SECONDS, "default": 120}, "execute": {"type": "boolean", "default": False}, "permission_grant": {"type": "string", "enum": ["user-authorized-local"]}}, ["command", "working_directory"]), read_only=False),
    "carl_enter_workshop": tool("Open Carl's Bethesda modding workshop with the Creation Kit skill receipt, supported games, callable lanes, and deterministic failure shield.", tool_carl_enter_workshop, schema({}), read_only=True),
    "carl_route": tool("Route Bethesda work across Creation Kit authoring, xEdit conflicts, manager deployment, crash isolation, Wabbajack modlists, and packaging with lane-specific gates.", tool_carl_route, schema({"task": {"type": "string", "minLength": 1, "maxLength": MAX_PROMPT_CHARS}}, ["task"]), read_only=True),
    "carl_plugin_audit": tool("Audit one Bethesda plugin's TES4 header, SHA-256, declared masters, presence, and optional active-state dependencies without changing it.", tool_carl_plugin_audit, schema({"plugin_path": {"type": "string", "minLength": 1, "maxLength": 1000}, "data_path": {"type": "string", "minLength": 1, "maxLength": 1000}, "plugins_path": {"type": "string", "maxLength": 1000}}, ["plugin_path", "data_path"]), read_only=True),
    "carl_creation_kit_audit": tool("Have Carl run a recent-change-first, read-only Creation Kit diagnostic. It parses Bethesda plugin master headers, checks active dependencies, separates native SFSE/SKSE/F4SE DLLs from Papyrus, and records every inspected path and check.", tool_carl_creation_kit_audit, schema({"game": {"type": "string", "enum": sorted(GAME_EXTENDERS)}, "data_path": {"type": "string", "minLength": 1, "maxLength": 1000}, "plugins_path": {"type": "string", "minLength": 1, "maxLength": 1000}, "recent_hours": {"type": "integer", "minimum": 1, "maximum": 720, "default": 72}, "recent_limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100}}, ["game", "data_path", "plugins_path"]), read_only=True),
    "carl_package_audit": tool("Inventory a Bethesda mod package without following directory links; classify plugins, archives, Papyrus, meshes, textures, and materials, hash bounded files, and parse plugin masters.", tool_carl_package_audit, schema({"package_path": {"type": "string", "minLength": 1, "maxLength": 1000}, "max_files": {"type": "integer", "minimum": 1, "maximum": 5000, "default": 2000}}, ["package_path"]), read_only=True),
    "nawnie_route": tool("Route a task through Nawnie's existing orchestrator and return explicit skill and specialist handoffs.", tool_route, schema({"task": {"type": "string", "minLength": 1, "maxLength": MAX_PROMPT_CHARS}}, ["task"]), read_only=True),
    "nawnie_call_agent": tool("Return a structured handoff to a registered Shawn Core specialist MCP server. This preserves each specialist's own tools and controls.", tool_call_agent, schema({"agent": {"type": "string"}, "task": {"type": "string", "minLength": 1, "maxLength": MAX_PROMPT_CHARS}}, ["agent", "task"]), read_only=True),
    "nawnie_spawn_agent": tool("Plan or explicitly start a local read-only Codex subagent with a selected model and reasoning effort. It waits for completion and returns only a bounded final handoff. execute defaults to false.", tool_spawn_agent, schema({"prompt": {"type": "string", "minLength": 1, "maxLength": MAX_PROMPT_CHARS}, "model": {"type": "string", "enum": sorted(ALLOWED_MODELS)}, "reasoning_effort": {"type": "string", "enum": sorted(ALLOWED_REASONING)}, "cwd": {"type": "string"}, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_TIMEOUT_SECONDS}, "execute": {"type": "boolean", "default": False}}, ["prompt"]), read_only=False),
    "nawnie_compare": tool("Plan or explicitly run a bounded local comparison between two supported Codex model routes under the same prompt and reasoning effort.", tool_compare, schema({"prompt": {"type": "string", "minLength": 1, "maxLength": MAX_PROMPT_CHARS}, "left_model": {"type": "string", "enum": sorted(ALLOWED_MODELS)}, "right_model": {"type": "string", "enum": sorted(ALLOWED_MODELS)}, "reasoning_effort": {"type": "string", "enum": sorted(ALLOWED_REASONING)}, "cwd": {"type": "string"}, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_TIMEOUT_SECONDS}, "execute": {"type": "boolean", "default": False}}, ["prompt"]), read_only=False),
}


def response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    request_id = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}
    if not isinstance(params, dict):
        return error(request_id, -32602, "params must be an object")
    if method == "initialize":
        return response(request_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": "Shawn Core is the always-on personal gateway unless Shawn opts out. Start with shawn_core_query (detail=compact for routine work). For errors call shawn_core_diagnose and then shawn_core_tool_help for invalid arguments. Use shawn_core_repair_open/note/transition/get to persist owned repair cases, test receipts, handoffs, and a learning note; the host must authenticate specialist identity independently. To run a named specialist in an independent read-only model subprocess, use shawn_core_specialist_agent with execute=true. Nawnie owns routing, durable state and redistribution. Specialists retain their own MCP tools. Use authorized host tools, skills and connectors under Nawnie when no specialist owns a capability. Return bounded results to shawn_core_validate; deterministic receipts gate acceptance.",
        })
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return response(request_id, {"tools": [{key: value for key, value in spec.items() if key != "handler"} | {"name": name} for name, spec in TOOLS.items()]})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments", {})
        checked = preflight_tool_call(name, arguments, TOOLS)
        if not isinstance(name, str) or name not in TOOLS:
            suggestion = f"; did you mean: {', '.join(checked['suggestions'])}" if checked["suggestions"] else ""
            return error(request_id, -32602, "; ".join(checked["errors"]) + suggestion)
        if not checked["valid"]:
            return response(request_id, {"content": [{"type": "text", "text": "; ".join(checked["errors"]) + "; use shawn_core_tool_help"}], "isError": True})
        try:
            payload = TOOLS[name]["handler"](arguments)
            return response(request_id, {"content": [{"type": "text", "text": json.dumps(payload, indent=2, sort_keys=True)}], "structuredContent": payload, "isError": False})
        except NawnieError as exc:
            return response(request_id, {"content": [{"type": "text", "text": str(exc)}], "isError": True})
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return response(request_id, {"content": [{"type": "text", "text": "Nawnie server error. Check local server stderr."}], "isError": True})
    return error(request_id, -32601, "method not found")


def main() -> int:
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            result = handle(request)
            if result is not None:
                print(json.dumps(result), flush=True)
        except Exception as exc:
            print(json.dumps(error(None, -32700, f"parse error: {exc}")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
