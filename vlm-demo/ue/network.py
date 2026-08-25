from __future__ import annotations

import subprocess


def validate_route(destination_ip: str, expected_interface: str, strict: bool, ue_ipv4: str) -> str:
    """Return `ip route get` output and require the OAI UE interface.

    This is deliberately read-only. It never changes the UE routing table. If the
    route is wrong, fix the OAI/container route explicitly rather than hiding that
    problem inside the demo application.
    """
    try:
        proc = subprocess.run(
            ["ip", "route", "get", destination_ip, "from", ue_ipv4],
            check=False,
            text=True,
            capture_output=True,
            timeout=3,
        )
    except FileNotFoundError as exc:
        message = "`ip` command not found; install iproute2"
        if strict:
            raise RuntimeError(message) from exc
        return message

    output = (proc.stdout or proc.stderr).strip()
    if proc.returncode != 0:
        message = f"route lookup failed for {destination_ip}: {output}"
        if strict:
            raise RuntimeError(message)
        return message


    token = f" dev {expected_interface} "
    padded = f" {output} "
    if token not in padded:
        message = (
            f"MEC route does not use expected UE interface {expected_interface!r}. "
            f"Actual route: {output}"
        )
        if strict:
            raise RuntimeError(message)
        return "WARNING: " + message

    return output


def get_interface_ipv4(interface: str) -> str:
    proc = subprocess.run(
        ["ip", "-4", "-o", "addr", "show", "dev", interface],
        check=False,
        text=True,
        capture_output=True,
        timeout=3,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())

    fields = proc.stdout.split()
    return fields[fields.index("inet") + 1].split("/", 1)[0]