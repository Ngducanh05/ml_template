from pathlib import Path
import io
import json
from unittest.mock import Mock
from control_plane.processor_v2 import S3Metadata
from control_plane.runtime_resolver import runtime_fingerprint
from control_plane.state_v2 import parse_event
from scripts.render_control_plane_v2 import render
from scripts.render_release_v2 import state_manifest
from test_processor_v2 import IMAGE


def test_real_producer_metadata_fixture_matches_published_fingerprint():
    root = Path(__file__).parents[1] / 'fixtures'
    event = parse_event(json.loads((root/'model_promoted_v1.json').read_text()))
    s3 = Mock()
    s3.get_object.side_effect = lambda **kw: {'Body':io.BytesIO((root/'runtime_v3'/kw['Key'].split('/')[-1]).read_bytes())}
    contract = S3Metadata(s3).inspect(event['artifact_uri'])
    assert runtime_fingerprint(contract) == 'sha256:700096f6d55f8aa9b56c9049e1c4171633f403a8d3e2a2de2df1ff544eab63fc'
    assert s3.get_object.call_count == 3


def test_rendered_control_plane_disabled_and_rbac_does_not_write_legacy():
    objects = render(IMAGE)['items']
    workloads = [x for x in objects if x['kind'] == 'Deployment']
    assert len(workloads) == 5 and all(x['spec']['replicas'] == 0 for x in workloads)
    for role in (x for x in objects if x['kind'] == 'Role'):
        configmap_rule = role['rules'][0]
        assert configmap_rule['resourceNames'] == ['model-release-state-v2']
        assert 'delete' not in {v for rule in role['rules'] for v in rule['verbs']}
    assert json.loads(state_manifest()['data']['state.json'])['stable'] is None

def test_ci_contract_validation_cannot_publish_or_build(tmp_path):
    import subprocess
    import sys
    import json
    from control_plane.build_contract import build_request
    from test_processor_v2 import CONTRACT
    request = tmp_path / 'request.json'
    request.write_text(json.dumps(build_request(CONTRACT)))
    args = [sys.executable, '-m', 'scripts.runtime_build_request', str(request)]
    validated = subprocess.run(args + ['--validate-only'], capture_output=True, text=True)
    assert validated.returncode == 0
    blocked = subprocess.run(args, capture_output=True, text=True)
    assert blocked.returncode != 0 and 'BUILD_REQUIRED' in blocked.stderr
