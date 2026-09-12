"""Read-only port validation for Shawn Core server creation workflows."""

from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path
from typing import Any


REGISTRY_PATH = Path(__file__).with_name("port_registry.json")
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class PortPolicyError(ValueError):
    pass


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("reservations"), list):
        raise PortPolicyError("Port registry is malformed")
    seen: set[int] = set()
    for item in value["reservations"]:
        port = int(item["port"])
        if port in seen or not 1 <= port <= 65535:
            raise PortPolicyError(f"Port registry contains an invalid or duplicate port: {port}")
        seen.add(port)
    return value


def live_listeners() -> list[dict[str, Any]]:
    if subprocess.os.name != "nt":
        return []
    script = (
        "$rows=@(Get-NetTCPConnection -State Listen | ForEach-Object {"
        "$p=Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue;"
        "[pscustomobject]@{address=$_.LocalAddress;port=$_.LocalPort;pid=$_.OwningProcess;process=$p.ProcessName}});"
        "$rows|ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        creationflags=CREATE_NO_WINDOW,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return []
    value = json.loads(completed.stdout)
    return value if isinstance(value, list) else [value]


def bind_available(port: int, bind: str) -> bool:
    host = "127.0.0.1" if bind == "loopback" else "0.0.0.0"
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def choose_port(
    preferred_port: int,
    range_start: int,
    range_end: int,
    *,
    reservations: dict[int, str],
    occupied_ports: set[int],
    reservation_name: str | None = None,
) -> int | None:
    if range_end < range_start or range_end - range_start > 1000:
        raise PortPolicyError("Fallback range must be ascending and at most 1001 ports")
    if not 1024 <= preferred_port <= 65535 or not 1024 <= range_start <= range_end <= 65535:
        raise PortPolicyError("Requested and fallback ports must be between 1024 and 65535")
    candidates = [preferred_port, *(port for port in range(range_start, range_end + 1) if port != preferred_port)]
    for port in candidates:
        fixed_owner = reservations.get(port)
        if fixed_owner and fixed_owner.casefold() != (reservation_name or "").casefold():
            continue
        if port not in occupied_ports:
            return port
    return None


def validate_port_request(
    *,
    service_name: str,
    preferred_port: int,
    range_start: int,
    range_end: int,
    bind: str,
    reservation_name: str | None = None,
    listeners: list[dict[str, Any]] | None = None,
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if bind not in {"loopback", "wildcard"}:
        raise PortPolicyError("bind must be loopback or wildcard")
    registry = registry or load_registry()
    used_live_probe = listeners is None
    listeners = live_listeners() if used_live_probe else listeners
    occupied_by_port: dict[int, list[dict[str, Any]]] = {}
    for item in listeners:
        occupied_by_port.setdefault(int(item["port"]), []).append(item)
    reservations = {int(item["port"]): str(item["name"]) for item in registry["reservations"]}
    chosen = choose_port(
        preferred_port,
        range_start,
        range_end,
        reservations=reservations,
        occupied_ports=set(occupied_by_port),
        reservation_name=reservation_name,
    )
    if chosen is not None and used_live_probe and not bind_available(chosen, bind):
        occupied_by_port.setdefault(chosen, []).append({"process": "unresolved", "port": chosen})
        chosen = choose_port(
            preferred_port,
            range_start,
            range_end,
            reservations=reservations,
            occupied_ports=set(occupied_by_port),
            reservation_name=reservation_name,
        )
    if chosen is None:
        raise PortPolicyError("No free port exists in the approved range")
    requested_owners = occupied_by_port.get(preferred_port, [])
    return {
        "schema": "shawn-core.port-validation.v1",
        "service_name": service_name,
        "requested_port": preferred_port,
        "assigned_port": chosen,
        "port_changed": chosen != preferred_port,
        "bind": bind,
        "bind_address": "127.0.0.1" if bind == "loopback" else "0.0.0.0",
        "requested_port_owners": requested_owners,
        "environment": {"PORT": str(chosen), "HOST": "127.0.0.1" if bind == "loopback" else "0.0.0.0"},
        "launch_rule": "Launch with the assigned port. Never kill the existing owner. Revalidate immediately before launch and fail closed if the assignment changed.",
        "firewall_change_authorized": False,
        "public_exposure_authorized": False,
    }
