"""Deadline tests use delayed fake inference, not claims about biometric accuracy."""
import time
from api.tests.test_decision import client, _start, _identity, _liveness
from api import main
from api.store import STORE


def test_slow_inference_cannot_allow_or_overwrite_final_result(client, monkeypatch):
    def slow(*args):
        time.sleep(.35)
        return ({'ok': True, 'continuity_ok': True},
                {'status': 'PASS', 'passive': {'status': 'LIVE', 'score': 1}}, {})
    monkeypatch.setattr(main, '_first_pass_pipeline', slow)
    aid = _start(client)
    _identity(client, aid)
    before = time.perf_counter()
    response = _liveness(client, aid, quick=True).json()
    assert response['decision'] == 'REQUEST_VERIFICATION'
    assert response['reason_code'] == 'TIMEOUT'
    assert time.perf_counter()-before < .3
    time.sleep(.4)
    assert STORE.get(aid).decision == response
    assert client.post(f'/attempt/{aid}/challenge/start').status_code == 200


def test_kiosk_and_enrollment_are_served_on_api_origin(client):
    assert client.get('/').status_code == 200
    assert client.get('/enroll.html').status_code == 200
    assert client.get('/.env').status_code == 404
