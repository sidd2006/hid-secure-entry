import io
import json

from fastapi.testclient import TestClient
from PIL import Image

from api import config, identity
from api.main import app
import pipeline.challenge as challenge


def test_enrollment_requires_operator_and_validates_id():
    c = TestClient(app)
    assert c.get('/enrollment/users').status_code == 403


def test_enrollment_persists_reference_and_policy_without_restart(monkeypatch, tmp_path):
    monkeypatch.setenv('ENROLLMENT_TOKEN', 'test-key')
    monkeypatch.setattr(config, 'ENROLL_DIR', tmp_path / 'references')
    monkeypatch.setattr(config, 'SCHEDULE_PATH', tmp_path / 'policy.json')
    monkeypatch.setattr(config, 'KIOSK_ZONE', None)
    monkeypatch.setattr(identity, '_ENROLLED', {})
    monkeypatch.setattr(identity, '_loaded', True)
    monkeypatch.setattr(challenge, 'metrics_for', lambda *a: {'n_faces': 1})
    image = io.BytesIO()
    Image.new('RGB', (160, 160), 'white').save(image, 'JPEG')
    c = TestClient(app)
    headers = {'X-Enrollment-Token': 'test-key'}
    data = dict(user_id='2580', consent='true', id_checked='true',
                start='08:00', end='20:00', days='MON,SAT', zone='main_gate', effect='approval')
    def submit():
        return c.post('/enrollment/users', headers=headers, data=data,
                      files={'photo': ('reference.jpg', image.getvalue(), 'image/jpeg')})
    data['id_checked'] = 'false'
    assert submit().status_code == 400
    data['id_checked'] = 'true'
    monkeypatch.setattr(challenge, 'metrics_for', lambda *a: {'n_faces': 2})
    assert submit().status_code == 400
    monkeypatch.setattr(challenge, 'metrics_for', lambda *a: {'n_faces': 1})
    assert submit().status_code == 200
    assert identity.reference_for('2580')
    assert (config.ENROLL_DIR / '2580.jpg').is_file()
    assert json.loads(config.SCHEDULE_PATH.read_text())['users']['2580']['windows'][0]['effect'] == 'approval'
    assert c.get('/enrollment/users', headers=headers).json()['users'] == ['2580']
    assert submit().status_code == 409
