from mec_ue.client import Client


def test_admission_bypass_requires_explicit_setting_and_registration():
    client = Client.__new__(Client)
    client.collection_mode = False
    client.association_required_for_admission = True
    assert not client.admission_trigger({'type': 'association_pending'})
    assert not client.admission_trigger({'type': 'association_status', 'valid': False})
    assert client.admission_trigger({'type': 'association_status', 'valid': True})
    client.association_required_for_admission = False
    assert client.admission_trigger({'type': 'association_pending'})
    assert not client.admission_trigger({'type': 'mec_hello'})
    assert not client.admission_trigger({'type': 'error'})
    client.collection_mode = True
    assert not client.admission_trigger({'type': 'association_pending'})
    assert not client.admission_trigger({'type': 'association_status', 'valid': True})
