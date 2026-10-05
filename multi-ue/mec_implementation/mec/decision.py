"""Decision contracts and optional RIC transport; execution stays elsewhere."""
import asyncio
import json
import time
import urllib.request
from typing import Literal

from .config import Schema


class Plan(Schema):
    accepted: bool
    config_id: str | None = None
    priority: Literal["high", "low"] = "low"
    reason: str | None = None


def post_json(url, body, timeout):
    request = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def without_rnti(value):
    if isinstance(value, dict):
        return {k: without_rnti(v) for k, v in value.items() if "rnti" not in k.lower()}
    if isinstance(value, list):
        return [without_rnti(v) for v in value]
    return value


class Telemetry:
    def __init__(self, cfg):
        self.cfg, self.latest, self.source_unix_ns = cfg, None, None

    def update(self, message):
        self.latest = without_rnti(message)
        # Require a SOURCE reporting timestamp; receiving a stalled cache must
        # never make it fresh. The xApp can add this field without changing gRPC.
        self.source_unix_ns = message.get("report_unix_ns")

    def snapshot(self):
        cfg = self.cfg
        current = int((time.time_ns() - cfg.period_origin_unix_ns) // (cfg.reporting_period_s * 1e9))
        index = None
        if isinstance(self.source_unix_ns, int):
            index = int((self.source_unix_ns - cfg.period_origin_unix_ns) // (cfg.reporting_period_s * 1e9))
        return {"fresh": index in {current, current - 1}, "period_index": index,
                "current_period_index": current, "reporting_period_s": cfg.reporting_period_s,
                "source_unix_ns": self.source_unix_ns, "data": self.latest}

    async def run(self, log):
        if not self.cfg.telemetry_target:
            return
        import grpc
        while True:
            try:
                async with grpc.aio.insecure_channel(self.cfg.telemetry_target) as channel:
                    await asyncio.wait_for(channel.channel_ready(), 5)
                    rpc = channel.unary_stream("/ran.telemetry.RANTelemetry/StreamTelemetry",
                        request_serializer=lambda d: json.dumps(d).encode(), response_deserializer=json.loads)
                    async for message in rpc({"interval_s": self.cfg.reporting_period_s}):
                        self.update(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.event("telemetry_unavailable", detail=repr(exc))
                await asyncio.sleep(self.cfg.reconnect_s)


class DecisionProvider:
    def __init__(self, cfg, catalog, telemetry, log):
        self.cfg, self.catalog, self.telemetry, self.log = cfg, catalog, telemetry, log

    def heuristic(self, request):
        choices = [c for c in self.catalog.configurations
                   if c.task_type == request.task_type and request.input_type in c.input_types]
        if not choices:
            return Plan(accepted=False, reason="unsupported_task")
        # Deliberately simple baseline: catalog order, independent of busy models.
        # Accuracy profiles are optional; this does not certify an accuracy SLO.
        return Plan(accepted=True, config_id=choices[0].config_id, priority="low")

    async def decide(self, task_id, request, mec_state, timeout):
        if self.cfg.decision_url:
            inputs = {"task_id": task_id, "request": request.model_dump(),
                      "mec": mec_state, "telemetry": self.telemetry.snapshot(),
                      "catalog": self.catalog.model_dump()}
            try:
                response = await asyncio.wait_for(asyncio.to_thread(post_json,
                    self.cfg.decision_url, inputs, timeout), timeout)
                # Require correlation so a late/incorrect RIC reply cannot commit.
                if response.get("task_id") != task_id:
                    raise ValueError("RIC response task_id mismatch")
                plan = Plan.model_validate(response["plan"])
                if plan.accepted:
                    app = self.catalog.get(plan.config_id)
                    if app.task_type != request.task_type or request.input_type not in app.input_types:
                        raise ValueError("RIC selected an incompatible configuration")
                return plan, "ric"
            except Exception as exc:
                self.log.event("ric_fallback", task_id=task_id, detail=repr(exc))
        return self.heuristic(request), "heuristic"

    async def associate(self, ue, timeout):
        if not self.cfg.identity_url:
            return
        body = {"ue_id": ue.ue_id, "ip_address": ue.reported_ip or ue.peer_ip,
                "connection_id": ue.connection_id, "updated_unix_ns": ue.last_seen["unix_ns"]}
        try:
            await asyncio.wait_for(asyncio.to_thread(post_json, self.cfg.identity_url, body, timeout), timeout)
        except Exception as exc:
            self.log.event("ric_association_unavailable", ue_id=ue.ue_id, detail=repr(exc))
