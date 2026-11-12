"""Guard the native-only source tree against obsolete PBX/CTI reintroduction."""
from pathlib import Path
import re

from app.config import APP_VERSION
from app.main import app

ROOT = Path(__file__).resolve().parents[1]


def test_native_only_structure_and_no_obsolete_bridge():
    obsolete = (
        'bridge', 'app/aura.py', 'AURA_DEPLOYMENT.md',
        'AURA_MEDIA_DEPLOYMENT.md', 'AVAYA_INTEGRATION.md',
        'telephony/demo_acd.py', 'telephony/sip_probe.py',
        'examples/normalize_event.py', 'examples/send_event.py',
        'tests/test_aura.py', 'tests/test_bridge.py',
    )
    assert not [name for name in obsolete if (ROOT / name).exists()]
    assert all((ROOT / name).is_file() for name in (
        'telephony/native_switch.py', 'telephony/sip_stack.py',
        'telephony/rtp_media.py', 'telephony/stereo_recording.py',
        'telephony/ivr.py', 'telephony/live_monitor.py',
        'telephony/acd.py', 'app/contact_center.py',
    ))


def test_dashboard_has_no_broken_direct_id_references():
    html = (ROOT / 'app/static/index.html').read_text(encoding='utf-8')
    javascript = (ROOT / 'app/static/app.js').read_text(encoding='utf-8')
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    referenced = set(re.findall(r"\$\(['\"]([^'\"]+)['\"]\)", javascript))
    assert referenced <= ids, f'Nonexistent HTML IDs: {referenced - ids}'


def test_runtime_requirements_are_not_test_tooling():
    runtime = (ROOT / 'requirements.txt').read_text(encoding='utf-8')
    dev = (ROOT / 'requirements-dev.txt').read_text(encoding='utf-8')
    ci = (ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8')
    assert 'pytest' not in runtime.lower()
    assert '-r requirements.txt' in dev
    assert 'pytest' in dev
    assert 'pip install -r requirements-dev.txt' in ci


def test_application_version_is_consistent():
    assert app.version == APP_VERSION == '1.4.0-lab'
