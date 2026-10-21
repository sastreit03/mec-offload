import csv
from muApp_mec.telemetry import TraceReader, Window


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
