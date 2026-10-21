from muApp_mec.association import Associations


def config():
    return dict(
        interval_s=0.2,
        lead_s=1,
        settle_s=0.4,
        rate_bps=1000000,
        min_correlation=0.8,
        min_margin=0.15,
        lease_s=30,
        revalidate_before_s=10,
        min_received_bytes=100,
    )


def exercise(a, now, ambiguous=False):
    c = a.next_probe(now)
    p = a.probe
    for i, on in enumerate(c["pattern"]):
        a.sample(
            p["start"] + (i + 0.5) * 0.2,
            {
                100: {"rx_bytes": 10 + 100 * on},
                200: {"rx_bytes": 10 + 100 * on if ambiguous else 20},
            },
        )
    a.receipt(c["ue_id"], c["probe_id"], 0, 1000)
    a.tick(p["finish"], {100, 200})
    return p["finish"]


def test_automatic_reassociation_on_rnti_ip_connection_and_lease_changes():
    a = Associations(config())
    a.register("u", "c", "12.1.1.2")
    now = exercise(a, 1)
    assert a.valid("u", now).rnti == 100
    a.tick(now + 1, {200})
    assert a.valid("u", now + 1) is None
    now = exercise(a, now + 2)
    assert a.valid("u", now)
    a.register("u", "c", "12.1.1.3")
    assert a.valid("u", now) is None
    now = exercise(a, now + 1)
    a.register("u", "new-connection", "12.1.1.3")
    assert a.valid("u", now) is None
    now = exercise(a, now + 1)
    a.tick(now + 31, {100})
    assert a.valid("u", now + 31) is None


def test_ambiguous_signature_retries_without_guessing():
    a = Associations(config())
    a.register("u", "c", "ip")
    now = exercise(a, 1, True)
    assert a.valid("u", now) is None and "u" in a.pending


def test_receipt_required_and_non_MEC_has_no_binding():
    a = Associations(config())
    a.register("u", "c", "ip")
    c = a.next_probe(1)
    p = a.probe
    for i, on in enumerate(c["pattern"]):
        a.sample(p["start"] + (i + 0.5) * 0.2, {99: {"rx_bytes": on}})
    a.tick(p["finish"], {99})
    assert not a.valid("u", p["finish"])
    assert set(a.bindings) == {"u"}


def test_AC05_old_association_cannot_rollback_replacement():
    a = Associations(config())
    a.register("u", "old", "ip1", 1)
    a.register("u", "new", "ip2", 2)
    a.register("u", "old", "ip1", 1)
    assert a.bindings["u"].connection_id == "new" and a.bindings["u"].ip == "ip2"


def test_old_ip_revision_cannot_rollback_same_socket_update():
    a = Associations(config())
    a.register("u", "c", "ip1", 1, 1)
    a.register("u", "c", "ip2", 1, 3)
    a.register("u", "c", "ip1", 1, 2)
    assert a.bindings["u"].ip == "ip2"
