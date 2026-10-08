"""CI-specific safety contracts; no infrastructure, registry or Docker required."""
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
import yaml
from scripts.ci_contracts import manifests, validate_manifests

ROOT = Path(__file__).resolve().parents[2]


def workflow():
    # BaseLoader preserves GitHub's YAML1.2 'on' key (PyYAML defaults to1.1).
    return yaml.load((ROOT/'.github/workflows/ci.yml').read_text(), Loader=yaml.BaseLoader)


def test_exact_ci_commands_render_and_validate(tmp_path):
    manifests(tmp_path)
    release = json.loads((tmp_path/'release.json').read_text())
    control = json.loads((tmp_path/'control-plane.json').read_text())
    validate_manifests(release, control)


@pytest.fixture
def rendered(tmp_path):
    manifests(tmp_path)
    return json.loads((tmp_path/'release.json').read_text()), json.loads((tmp_path/'control-plane.json').read_text())


@pytest.mark.parametrize('violation', ['replica','legacy-state','delete','lane','fingerprint','mutable','storage','reader','stable','legacy-url','wildcard'])
def test_manifest_guard_rejects_dangerous_drift(rendered, violation):
    release, control = rendered
    state = release['items'][0]
    runtime = next(x for x in release['items'] if x['kind']=='ServingRuntime')
    lane = next(x for x in release['items'] if x['kind']=='InferenceService')
    role = next(x for x in control['items'] if x['kind']=='Role')
    deployment = next(x for x in control['items'] if x['kind']=='Deployment')
    if violation == 'replica': deployment['spec']['replicas'] = 1
    elif violation == 'legacy-state': role['rules'][0]['resourceNames'] = ['model-release-state']
    elif violation == 'delete': role['rules'][0]['verbs'].append('delete')
    elif violation == 'lane': lane['metadata']['name'] = 'credit-risk-classifier-stable'
    elif violation == 'fingerprint': runtime['metadata']['annotations']['ml-template.io/runtime-fingerprint'] = 'sha256:short'
    elif violation == 'mutable': runtime['spec']['containers'][0]['image'] = 'runtime:latest'
    elif violation == 'storage': lane['spec']['predictor']['model']['storageUri'] = 's3://wrong/key'
    elif violation == 'reader': lane['spec']['predictor']['serviceAccountName'] = 'default'
    elif violation == 'stable':
        data = json.loads(state['data']['state.json']); data['stable'] = {}; state['data']['state.json'] = json.dumps(data)
    elif violation == 'legacy-url': deployment['spec']['template']['spec']['containers'][0]['env'].append({'name':'V2_ROUTER_URL','value':'http://credit-risk-router:8080'})
    elif violation == 'wildcard': role['rules'][0]['resources'] = ['*']
    with pytest.raises(ValueError): validate_manifests(release, control)


def test_workflow_trigger_permissions_and_isolation():
    data = workflow()
    assert data['on'] == {'pull_request': {'branches':['main']}, 'push': {'branches':['main']}}
    assert data['permissions'] == {'contents':'read'}
    assert data['concurrency'] == {'group':'${{ github.workflow }}-${{ github.ref }}','cancel-in-progress':'true'}
    assert data['env']['PYTHONPATH'] == '.:src'
    assert len(data['jobs']) == 7
    for job in data['jobs'].values():
        assert job['runs-on'] == 'ubuntu-latest'
        assert int(job['timeout-minutes']) > 0
        assert not any(k in job for k in ('environment','permissions','uses','continue-on-error'))
        for step in job['steps']:
            assert 'continue-on-error' not in step
            if step.get('uses','').startswith('actions/checkout@'):
                assert step['with']['persist-credentials'] == 'false'
    text = (ROOT/'.github/workflows/ci.yml').read_text()
    for banned in ('pull_request_target','secrets.','aws-actions/','self-hosted','kubectl ', 'docker login',
                   'docker push', 'id-token:', 'KUBECONFIG', 'release.yml', 'cd-local.yml','runtime-build.yml'):
        assert banned not in text
    scan = next(s for s in data['jobs']['control-plane-image-security']['steps'] if s.get('uses','').startswith('aquasecurity/'))
    assert scan['uses'] == 'aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25'
    assert scan['with']['version'] == 'v0.70.0'
    assert scan['with']['exit-code'] == '1' and scan['with']['ignore-unfixed'] == 'false'
    assert scan['with']['severity'] == 'HIGH,CRITICAL'
    assert scan['env']['TRIVY_IMAGE_SRC'] == 'docker'
    assert scan['env']['TRIVY_IGNOREFILE'] == '/dev/null'


@pytest.mark.parametrize('result', ['success','failure','cancelled','skipped','missing'])
def test_actual_final_gate_script_fails_closed(result):
    gate = workflow()['jobs']['ci-gate']
    assert gate['if'] == 'always()'
    assert set(gate['needs']) == set(workflow()['jobs']) - {'ci-gate'}
    script = gate['steps'][0]['run'].split("python - <<'PY'\n",1)[1].rsplit('\nPY',1)[0]
    states = {job:{'result':'success'} for job in gate['needs']}
    if result == 'missing': states.pop(gate['needs'][0])
    else: states[gate['needs'][0]]['result'] = result
    output = subprocess.run([sys.executable,'-c',script], env={**os.environ,'NEEDS_JSON':json.dumps(states)}, capture_output=True, text=True)
    assert (output.returncode == 0) == (result == 'success')


@pytest.mark.parametrize("name", ["model-promotion-consumer-v2", "model-runtime-processor-v2",
    "model-champion-reconciler-v2", "model-release-controller-v2", "credit-risk-router-v2"])
@pytest.mark.parametrize("violation", ["account", "command", "args", "nonroot", "uid", "gid", "seccomp",
    "escalation", "readonly", "capabilities", "privileged", "override", "sidecar", "role", "subject-kind",
    "subject-name", "subject-namespace", "extra-subject"])
def test_workload_identity_security_drift(rendered, name, violation):
    release, control = rendered
    deployment = next(x for x in control["items"] if x["kind"] == "Deployment" and x["metadata"]["name"] == name)
    binding = next(x for x in control["items"] if x["kind"] == "RoleBinding" and x["metadata"]["name"] == name)
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    if violation == "account": pod["serviceAccountName"] = "default"
    elif violation == "command": container["command"] = ["python", "-m", "control_plane.controller"]
    elif violation == "args": container["args"] = ["wrong-role", "--enable-live"]
    elif violation == "nonroot": pod["securityContext"]["runAsNonRoot"] = False
    elif violation == "uid": pod["securityContext"]["runAsUser"] = 0
    elif violation == "gid": pod["securityContext"]["runAsGroup"] = 0
    elif violation == "seccomp": pod["securityContext"]["seccompProfile"] = {"type": "Unconfined"}
    elif violation == "escalation": container["securityContext"]["allowPrivilegeEscalation"] = True
    elif violation == "readonly": container["securityContext"]["readOnlyRootFilesystem"] = False
    elif violation == "capabilities": container["securityContext"]["capabilities"]["add"] = ["SYS_ADMIN"]
    elif violation == "privileged": container["securityContext"]["privileged"] = True
    elif violation == "override": container["securityContext"]["runAsUser"] = 0
    elif violation == "sidecar": pod["containers"].append(dict(container))
    elif violation == "role": binding["roleRef"]["name"] = "legacy-role"
    elif violation == "subject-kind": binding["subjects"][0]["kind"] = "User"
    elif violation == "subject-name": binding["subjects"][0]["name"] = "default"
    elif violation == "subject-namespace": binding["subjects"][0]["namespace"] = "default"
    elif violation == "extra-subject": binding["subjects"].append(dict(binding["subjects"][0]))
    with pytest.raises(ValueError):
        validate_manifests(release, control)
