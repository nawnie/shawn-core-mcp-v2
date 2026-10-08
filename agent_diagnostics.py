"""Read-only, evidence-first diagnosis and MCP tool-call preflight.

No diagnosis is proof: these functions propose falsifiable hypotheses. They do not
execute tools, mutate arguments, run commands, or mark a repair complete.
"""
from __future__ import annotations

from difflib import get_close_matches
from typing import Any
import re

CATEGORIES: tuple[dict[str, Any], ...] = (
    {
        "id": "tool-serialization",
        "owner": "al",
        "signals": ("chat template", "tool parser", "tool call", "function call", "json parse",
                    "invalid json", "malformed arguments", "wrong tool", "unexpected tool",
                    "arguments_json", "unknown tool"),
        "probe": "Capture the raw assistant output, the model chat template, advertised schema and parsed tool-call object; compare them.",
        "possible_repair": "Fix the template/parser or schema mapping before changing the model.",
    },
    {
        "id": "tool-execution",
        "owner": "operator",
        "signals": ("tool fails", "command failed", "exit code", "timeout", "file not found",
                    "not found", "permission denied", "exception", "subprocess"),
        "probe": "Reproduce the exact tool call with the same arguments, directory, identity and permissions; record exit status and stderr.",
        "possible_repair": "Repair the executor, environment or permission boundary; do not invent tool success.",
    },
    {
        "id": "observation-state",
        "owner": "al",
        "signals": ("wrong screen", "stale state", "ram", "screenshot", "vision",
                    "can't see", "cannot see", "yes/no", "pixel", "observation", "game state"),
        "probe": "Timestamp the observation, correlate with environment/RAM ground truth, execute one deterministic action, then re-observe.",
        "possible_repair": "Repair the sensor/state extractor or feedback timing before retraining.",
    },
    {
        "id": "memory-context",
        "owner": "nawnie",
        "signals": ("forgot", "memory", "context", "lost goal", "repeat", "restarted",
                    "checkpoint", "resume", "stale fact", "hallucinated success"),
        "probe": "Restart mid-task; compare the persisted checkpoint, objective, tool-result ledger and next action against the original run.",
        "possible_repair": "Persist authoritative state and idempotency keys; keep retrieved context separate from canonical records.",
    },
    {
        "id": "runtime-resources",
        "owner": "al",
        "signals": ("cuda", "oom", "out of memory", "vram", "kv cache", "offload",
                    "slow inference", "tok/s", "gpu", "quantization", "quantized"),
        "probe": "Record exact model artifact, runtime/backend version, context length, concurrency, peak VRAM, TTFT and throughput.",
        "possible_repair": "Fix resource provisioning, context/cache strategy, runtime compatibility or scheduling before scaling model size.",
    },
    {
        "id": "model-capability",
        "owner": "al",
        "signals": ("bad reasoning", "model not smart", "needs bigger model", "poor accuracy",
                    "model fails", "prompt", "sampling", "temperature"),
        "probe": "Run an identical frozen test set with correct observations/tools across baseline and alternate models; measure task success.",
        "possible_repair": "Improve prompting/decoding or select another model only after external layers pass.",
    },
    {
        "id": "training-evaluation",
        "owner": "al",
        "signals": ("fine tune", "finetune", "lora", "qlora", "train", "dataset",
                    "overfit", "loss curve", "holdout", "eval"),
        "probe": "Check data splits for leakage, evaluate a held-out baseline, train once, reload the artifact and rerun the same evaluation.",
        "possible_repair": "Fix dataset, labels, evaluation or optimization, not just the training loss.",
    },
    {
        "id": "authorization",
        "owner": "agent-t",
        "signals": ("prompt injection", "unauthorized", "credential", "token leak",
                    "exfiltrate", "policy bypass", "tool permission", "access denied"),
        "probe": "Check the host's actual authorization decision and scoped credentials with an untrusted tool-result injection test.",
        "possible_repair": "Enforce capabilities and scope at the executor; never trust a model's self-reported authorization.",
    },
    {
        "id": "frontend-integration",
        "owner": "wren",
        "signals": ("react", "frontend", "websocket", "stream ui", "render",
                    "browser", "web ui", "chat interface", "ui state"),
        "probe": "Trace one request from UI event through API contract, stream chunks, rendering, error state and user-visible completion.",
        "possible_repair": "Correct the frontend/API contract or event handling without changing inference internals.",
    },
)

def _mentions(haystack: str, needle: str) -> bool:
    return bool(re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", haystack))

def diagnose(symptom: str, evidence: list[str] | None = None) -> dict[str, Any]:
    """Rank candidate failure *layers*, not asserted root causes."""
    if not isinstance(symptom, str) or not symptom.strip() or len(symptom) > 4000:
        raise ValueError("symptom must be a non-empty string <= 4000 characters")
    if evidence is None:
        evidence = []
    if not isinstance(evidence, list) or len(evidence) > 20 or any(
        not isinstance(item, str) or len(item) > 2000 for item in evidence
    ):
        raise ValueError("evidence must contain <= 20 strings, each <= 2000 characters")
    combined = (" ".join([symptom, *evidence])).casefold()
    scores = []
    for rule in CATEGORIES:
        matched = [signal for signal in rule["signals"] if _mentions(combined, signal)]
        if matched:
            scores.append((len(matched), rule, matched))
    scores.sort(key=lambda item: -item[0])
    candidates = [
        {
            "layer": rule["id"],
            "owner": rule["owner"],
            "matched_signals": matched,
            "status": "hypothesis-not-verified",
            "discriminating_test": rule["probe"],
            "possible_repair_after_verification": rule["possible_repair"],
        }
        for _, rule, matched in scores[:3]
    ]
    return {
        "schema": "shawn-core.diagnosis.v1",
        "symptom": symptom,
        "evidence_supplied": bool(evidence),
        "root_cause_verified": False,
        "candidates": candidates,
        "fallback": None if candidates else {
            "owner": "researcher",
            "action": "Collect one reproducible failing request, exact expected/actual result, model/runtime versions and tool trace before assigning a repair.",
        },
        "acceptance": "Verifier must rerun the original failure and a regression case; an agent's statement of success is not evidence.",
    }

def _matches_type(value: Any, expected: str) -> bool:
    return {
        "string": lambda: isinstance(value, str),
        "object": lambda: isinstance(value, dict),
        "array": lambda: isinstance(value, list),
        "integer": lambda: type(value) is int,
        "number": lambda: type(value) in (int, float),
        "boolean": lambda: type(value) is bool,
        "null": lambda: value is None,
    }.get(expected, lambda: True)()

def preflight_tool_call(name: str, arguments: Any, catalog: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Validate against the advertised subset of JSON Schema without rewriting input."""
    if not isinstance(name, str) or not name.strip():
        return {"valid": False, "errors": ["tool name must be a non-empty string"], "suggestions": []}
    if name not in catalog:
        suggestions = get_close_matches(name, list(catalog), n=4, cutoff=0.45)
        return {"valid": False, "errors": [f"unknown tool: {name}"], "suggestions": suggestions}
    if not isinstance(arguments, dict):
        return {"valid": False, "errors": ["tool arguments must be a JSON object"], "suggestions": [name]}
    spec = catalog[name].get("inputSchema", {})
    props = spec.get("properties", {})
    errors = []
    for key in spec.get("required", []):
        if key not in arguments:
            errors.append(f"missing required argument: {key}")
    if spec.get("additionalProperties") is False:
        for key in arguments:
            if key not in props:
                close = get_close_matches(key, list(props), n=1, cutoff=0.6)
                errors.append(f"unsupported argument: {key}" + (f" (did you mean {close[0]}?)" if close else ""))
    for key, value in arguments.items():
        field = props.get(key)
        if not isinstance(field, dict):
            continue
        expected = field.get("type")
        if isinstance(expected, str) and not _matches_type(value, expected):
            errors.append(f"{key} must be {expected}")
            continue
        if "enum" in field and value not in field["enum"]:
            errors.append(f"{key} must be one of: {field['enum']}")
        if isinstance(value, str):
            if "minLength" in field and len(value) < field["minLength"]:
                errors.append(f"{key} must have at least {field['minLength']} characters")
            if "maxLength" in field and len(value) > field["maxLength"]:
                errors.append(f"{key} must have at most {field['maxLength']} characters")
        if type(value) in (int, float):
            if "minimum" in field and value < field["minimum"]:
                errors.append(f"{key} must be >= {field['minimum']}")
            if "maximum" in field and value > field["maximum"]:
                errors.append(f"{key} must be <= {field['maximum']}")
        if isinstance(value, list):
            if "maxItems" in field and len(value) > field["maxItems"]:
                errors.append(f"{key} must have <= {field['maxItems']} items")
            if "minItems" in field and len(value) < field["minItems"]:
                errors.append(f"{key} must have >= {field['minItems']} items")
    return {
        "valid": not errors,
        "errors": errors,
        "suggestions": [],
        "schema": spec,
        "note": "Preflight supports common schema constraints only; handler and server authorization remain authoritative.",
    }
