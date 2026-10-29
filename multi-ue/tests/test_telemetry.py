import csv
from types import SimpleNamespace
from muApp_mec.telemetry import TraceReader, Window
from muApp_mec.runtime import Runtime
from muApp_mec.demand import Ledger
from muApp_mec.legacy_wire import metrics_pb2


def test_trace_measurement_clock_conversion_and_partial_lines(tmp_path):
    p = tmp_path / "t.csv"
    header = [
        "kind",
        "time_ns",
        "direction",
        "rnti",
        "stage",
        "prbs",
        "tbs",
        "retransmission",
        "reference",
        "target_frame",
        "target_slot",
        "beam",
        "dropped",
        "io_errors",
        "weight",
    ]
    row = [3, 100, 1, 7, 9, 10, 1000, 0, 50, 1, 2, 0, 0, 0, 0.5]
    p.write_text(
        "# OAI scheduler trace v2; pid=1; monotonic_ns=10; realtime_ns=1010\n"
        + ",".join(header)
        + "\n"
        + ",".join(map(str, row))
        + "\n3,partial"
    )
    r = TraceReader(p)
    records = r.poll()
    assert len(records) == 1 and r.mapping == 1000 and r.coverage_valid
    assert not r.poll()
    w = Window(0, 1000)
    w.add(records[0])
    record = w.record()
    assert record["allocated_by_rnti"][7]["tbs_bytes"] == 1000
    assert record["total_ul_opportunity_valid"] is False
    retry = dict(records[0], retransmission=1)
    w.add(retry)
    assert w.record()["allocated_by_rnti"][7]["retry_tbs_bytes"] == 1000


def test_generation_changes_on_truncation(tmp_path):
    p = tmp_path / "t"
    p.write_text("# begin\n")
    r = TraceReader(p)
    r.poll()
    g = r.generation
    p.write_text("")
    r.poll()
    assert r.generation > g


def test_aetheredge_zmq_capacity_and_cumulative_grant_deltas():
    # Aetheredge: Validate the wire contract and loss-proof delta accounting together.
    message = metrics_pb2.Metrics(source_generation=9, report_sequence=1)
    ue = message.ue_metrics.add(
        rnti=7,
        capacity_valid=True,
        capacity_bytes_per_prb=125.0,
        reference_prbs=4,
        reference_tbs_bits=4000,
        new_data_prbs_total=10,
        new_data_tbs_bytes_total=1000,
        retransmission_prbs_total=2,
        retransmission_tbs_bytes_total=200,
        radio_state_valid=True,
        radio_state_frame=100,
        radio_state_slot=7,
        scheduler_ul_mcs=12,
        scheduler_dl_mcs=18,
        ul_bler=0.1,
        dl_bler=0.2,
        pusch_snr_db=14.5,
        pucch_snr_db=12.0,
        dl_new_data_prbs_total=20,
        dl_new_data_tbs_bytes_total=2400,
        dl_retransmission_prbs_total=1,
        dl_retransmission_tbs_bytes_total=120,
        dl_rlc_buffer_bytes=4096,
        dl_rlc_interface_sdu_bytes_total=8000,
        ul_rlc_interface_sdu_bytes_total=7000,
        rlc_counters_valid=True,
    )
    decoded = metrics_pb2.Metrics.FromString(message.SerializeToString())
    assert decoded.ue_metrics[0].reference_tbs_bits == 4000

    runtime = Runtime.__new__(Runtime)
    runtime.cfg = {"payload_fraction": 1.0}
    runtime.adapter = SimpleNamespace(source_generation=9, report_sequence=1)
    runtime.metrics = {7: {field.name: getattr(ue, field.name) for field in ue.DESCRIPTOR.fields}}
    runtime.radio_generation = None
    runtime.radio_sequence = None
    runtime.radio_counters = {}
    runtime.capacity_samples = {}
    runtime.radio_coverage_valid = False
    runtime.capacity = {}
    ledger = Ledger()
    ledger.descriptor(1, 1, 10_000, 2000, True)
    runtime.sessions = {"video": {"ue_id": "ue", "ledger": ledger}}
    runtime.associations = SimpleNamespace(
        bindings={},
        invalidate=lambda *_: None,
        valid=lambda *_: SimpleNamespace(rnti=7),
    )
    runtime.window = Window(0, 100, "oai_zmq_cumulative_v1")
    runtime.event = lambda *_args, **_kwargs: None

    runtime._consume_ran_sample(1.0)
    assert runtime.capacity[7][0] == 125.0
    assert runtime.window.record()["allocated_by_rnti"] == {}

    runtime.adapter.report_sequence = 2
    runtime.metrics[7]["new_data_prbs_total"] = 13
    runtime.metrics[7]["new_data_tbs_bytes_total"] = 1600
    runtime.metrics[7]["retransmission_prbs_total"] = 4
    runtime.metrics[7]["retransmission_tbs_bytes_total"] = 500
    runtime.metrics[7]["dl_new_data_prbs_total"] = 25
    runtime.metrics[7]["dl_new_data_tbs_bytes_total"] = 3000
    runtime.metrics[7]["dl_retransmission_prbs_total"] = 3
    runtime.metrics[7]["dl_retransmission_tbs_bytes_total"] = 360
    runtime._consume_ran_sample(1.1)
    allocated = runtime.window.record()["allocated_by_rnti"][7]
    assert allocated == {
        "prbs": 3,
        "tbs_bytes": 600,
        "retry_prbs": 2,
        "retry_tbs_bytes": 300,
        "dl_prbs": 5,
        "dl_tbs_bytes": 600,
        "dl_retry_prbs": 2,
        "dl_retry_tbs_bytes": 240,
    }
    # Aetheredge: Only 600 new-data bytes debit demand; retry bytes remain telemetry.
    assert ledger.frames[1].remaining == 1400

    binding = SimpleNamespace(connection_id="connection", generation=2, rnti=7)
    runtime.associations.bindings = {"ue": binding}
    runtime.associations.valid = lambda *_: binding
    runtime.window_ns = 100
    runtime.run_id = "run"
    runtime.source = "edge-generation"
    runtime.sequence = 0
    runtime.last_index = 55
    runtime.latest_metric = 1.1
    events = []
    runtime.event = lambda kind, **fields: events.append((kind, fields))
    runtime.publish(1.2)

    report = runtime.latest_report
    ue_report = report["ues"]["ue"]
    assert "rnti" not in ue_report
    assert ue_report["radio_state"]["scheduler_ul_mcs"] == 12
    assert ue_report["cumulative_grants"]["dl_new_data_prbs_total"] == 25
    assert ue_report["rlc_counters"] == {
        "valid": True,
        "dl_buffer_bytes": 4096,
        "buffer_layer": "RLC",
        "dl_interface_sdu_bytes_total": 8000,
        "ul_interface_sdu_bytes_total": 7000,
        "throughput_boundary": "MAC/RLC interface",
    }
    # C-RNTI is retained only in EdgeRIC's local event evidence.
    assert events[-1][1]["report"]["ues"]["ue"]["rnti"] == 7
