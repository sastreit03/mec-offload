from __future__ import annotations

import subprocess


def validate_route(destination_ip: str, expected_interface: str | None, strict: bool) -> str:
    """Return `ip route get` output and optionally require the OAI UE interface.

    This is deliberately read-only. It never changes the UE routing table. If the
    route is wrong, fix the OAI/container route explicitly rather than hiding that
    problem inside the demo application.
    """
    try:
        proc = subprocess.run(
            ["ip", "route", "get", destination_ip],
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

    if expected_interface:
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
