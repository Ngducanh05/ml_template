import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock
import pytest
from control_plane.runtime_security import evaluate_scan, load_json, UPSTREAM, RUNTIME, BASE, REVIEW
from control_plane.build_contract import build_request
from control_plane.runtime_resolver import ArtifactContract, RUNTIME_CI_V2, normalize_requirements
from scripts.runtime_ci import publish
from test_runtime_ci import REQUEST, IMAGE, CONFIG

NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)
EMPTY = {'schema_version': 1, 'waivers': []}


def report(severity='HIGH', fixed='', package='mlflow', os=False):
    return {'ArtifactType': 'container_image', 'Metadata': {'ImageID': CONFIG}, 'Results': [
        {'Target': 'debian' if os else 'Python', 'Type': 'debian' if os else 'python-pkg',
         'Class': 'os-pkgs' if os else 'lang-pkgs', 'Vulnerabilities': [
             {'VulnerabilityID': 'CVE-test', 'PkgName': package, 'InstalledVersion': '3.14.0',
              'Severity': severity, 'FixedVersion': fixed}]}]}


def waiver():
    return {'schema_version': 1, 'waivers': [{'vulnerability_id': 'CVE-test', 'package_name': 'mlflow',
        'installed_version': '3.14.0', 'build_profile': RUNTIME_CI_V2.build_profile,
        'reason': 'Reviewed test fixture only', 'compensating_controls': 'Explicit test review',
        'owner': 'test-owner', 'expires_at': '2026-10-10T00:00:00Z'}]}


def evaluate(r, w=EMPTY, request=REQUEST):
    return evaluate_scan(r, request, w, now=NOW)


@pytest.mark.parametrize('r', [{}, {'ArtifactType': 'container_image'}, {**report(), 'Results': []},
    {**report(), 'Metadata': {'ImageID': 'not-a-digest'}}, {**report(), 'Results': [None]}])
def test_incomplete_scan_blocks(r):
    assert evaluate(r)['decision'] == 'BLOCK'
    assert evaluate(r)['errors']


@pytest.mark.parametrize('field,value', [('Severity','NEGLIGIBLE'),('PkgName',''),('InstalledVersion',None),
                                       ('VulnerabilityID',1),('FixedVersion',[]),('Severity',None)])
def test_malformed_record_fails_closed(field,value):
    r=report(); r['Results'][0]['Vulnerabilities'][0][field]=value
    assert evaluate(r)['errors']


def test_critical_direct_pin_remediation():
    d=evaluate(report('CRITICAL','3.15.0'))
    assert d['decision']=='BLOCK'
    assert d['blocking_findings'][0]['ownership']==UPSTREAM
    assert d['remediation'][UPSTREAM][0]['fixed_version']=='3.15.0'


@pytest.mark.parametrize('severity,fixed,os', [('CRITICAL','',True),('CRITICAL','3.15',True),
    ('HIGH','3.15',True),('UNKNOWN','3.15',True),('HIGH','',False),('UNKNOWN','',False)])
def test_forbidden_waiver_cannot_authorize(severity,fixed,os):
    d=evaluate(report(severity,fixed,os=os),waiver())
    assert d['decision']=='BLOCK' and d['errors']


@pytest.mark.parametrize('severity', ['HIGH','UNKNOWN'])
def test_os_unfixed_requires_exact_unexpired_waiver(severity):
    r=report(severity,os=True)
    assert evaluate(r)['remediation'][REVIEW]
    d=evaluate(r,waiver())
    assert d['decision']=='PASS' and len(d['waived_findings'])==1
    for field in ('vulnerability_id','package_name','installed_version','build_profile'):
        w=waiver();w['waivers'][0][field]+='different'
        assert evaluate(r,w)['decision']=='BLOCK'


@pytest.mark.parametrize('change', ['expired','wildcard','duplicate','missing','naive','not-list'])
def test_invalid_waivers_fail_closed(change):
    w=waiver()
    if change=='expired':w['waivers'][0]['expires_at']='2026-10-09T00:00:00Z'
    if change=='wildcard':w['waivers'][0]['package_name']='*'
    if change=='duplicate':w['waivers'].append(copy.deepcopy(w['waivers'][0]))
    if change=='missing':del w['waivers'][0]['owner']
    if change=='naive':w['waivers'][0]['expires_at']='2026-10-10T00:00:00'
    if change=='not-list':w['waivers']={}
    assert evaluate(report(os=True),w)['errors']


@pytest.mark.parametrize('severity', ['HIGH','UNKNOWN'])
@pytest.mark.parametrize('os', [True,False])
def test_fixable_blocks_with_correct_owner(severity,os):
    d=evaluate(report(severity,'patched',package='other',os=os))
    assert d['decision']=='BLOCK'
    assert d['blocking_findings'][0]['remediation']==(BASE if os else RUNTIME)


def test_unfixed_python_direct_blocks():
    assert evaluate(report())['remediation'][UPSTREAM]
    assert evaluate(report(package='transitive'))['remediation'][RUNTIME]


@pytest.mark.parametrize('severity', ['LOW','MEDIUM'])
def test_nonblocking_evidence(severity):
    d=evaluate(report(severity))
    assert d['decision']=='PASS' and len(d['non_blocking_findings'])==1


def test_counts_sorting_and_duplicate_findings_deterministic():
    r=report();v=r['Results'][0]['Vulnerabilities'][0]
    r['Results'][0]['Vulnerabilities']=[dict(v,PkgName='z'),dict(v,PkgName='a'),dict(v,PkgName='a')]
    d=evaluate(r);r['Results'][0]['Vulnerabilities'].reverse()
    assert d==evaluate(r)
    assert d['counts']['raw']['HIGH']==3 and d['counts']['unique_vulnerabilities']==1


def test_ownership_comes_from_request_not_package_name():
    request=build_request(ArtifactContract('3.12.14',normalize_requirements('custom-library==3.14.0')),RUNTIME_CI_V2)
    assert evaluate(report('CRITICAL',package='custom_library'),request=request)['remediation'][UPSTREAM]
    assert evaluate(report('CRITICAL'),request=request)['remediation'][RUNTIME]


def test_publication_rechecks_valid_blocking_record_before_registry():
    catalog=Mock()
    with pytest.raises(ValueError,match='Security gate'):publish(REQUEST,IMAGE,report('CRITICAL','3.15'),catalog)
    assert not catalog.mock_calls


def test_cli_preserves_block_evidence_before_exit(tmp_path):
    req=tmp_path/'request.json';req.write_text(json.dumps(REQUEST))
    scan=tmp_path/'trivy.json';scan.write_text(json.dumps(report('CRITICAL','3.15')))
    out=tmp_path/'decision.json'
    r=subprocess.run([sys.executable,'-m','scripts.runtime_security','--request',str(req),'--scan',str(scan),'--output',str(out)],capture_output=True)
    assert r.returncode==1 and json.loads(out.read_text())['remediation'][UPSTREAM]
    scan.unlink()
    r=subprocess.run([sys.executable,'-m','scripts.runtime_security','--request',str(req),'--scan',str(scan),'--output',str(out)],capture_output=True)
    assert r.returncode==1 and json.loads(out.read_text())['errors']


def test_duplicate_json_keys_rejected(tmp_path):
    p=tmp_path/'waivers.json';p.write_text('{"waivers":[],"waivers":[]}')
    with pytest.raises(ValueError,match='Duplicate'):load_json(p)


def test_workflow_preserves_decision_but_not_blocked_image():
    import yaml
    w=yaml.load(Path('.github/workflows/runtime-build.yml').read_text(),Loader=yaml.BaseLoader)
    steps=w['jobs']['build']['steps']
    gate=next(s for s in steps if 'scripts.runtime_security' in s.get('run',''))
    evidence=next(s for s in steps if s.get('with',{}).get('name','').startswith('runtime-security-'))
    archive=next(s for s in steps if s.get('with',{}).get('name')=='runtime-tested')
    assert gate['if']=='always()' and evidence['if']=='always()'
    assert steps.index(gate)<steps.index(evidence)<steps.index(archive)
    assert 'runtime.tar' not in evidence['with']['path']
    assert 'security-decision.json' in evidence['with']['path']
    assert 'security-decision.json' in archive['with']['path']
    assert 'if' not in archive and 'continue-on-error' not in gate
    assert 'needs.build.result == \'success\'' in w['jobs']['publish']['if']


def test_publication_accepts_only_eligible_waiver_and_keeps_identity_checks(tmp_path, monkeypatch):
    import scripts.runtime_ci as ci
    (tmp_path/'security').mkdir()
    w=waiver();w['waivers'][0]['expires_at']='2099-01-01T00:00:00Z'
    (tmp_path/'security/runtime-vulnerability-waivers.json').write_text(json.dumps(w))
    monkeypatch.setattr(ci,'ROOT',tmp_path)
    c=Mock();c.lookup.return_value=None;c.publish.return_value=IMAGE
    c.ecr.batch_get_image.return_value={'images':[{'imageId':{'imageDigest':IMAGE.split('@')[1]},'imageManifest':json.dumps({'config':{'digest':CONFIG}})}]}
    assert publish(REQUEST,IMAGE,report(os=True),c)==IMAGE
    c.publish.assert_called_once_with(REQUEST['fingerprint'],IMAGE)
    c.reset_mock();c.ecr.batch_get_image.return_value['images'][0]['imageManifest']=json.dumps({'config':{'digest':'wrong'}})
    with pytest.raises(ValueError,match='scanned'):publish(REQUEST,IMAGE,report(os=True),c)
    c.publish.assert_not_called()


def test_publication_cli_blocks_before_aws_or_docker(tmp_path,monkeypatch):
    import scripts.runtime_ci_publish as cli
    req=tmp_path/'request.json';req.write_text(json.dumps(REQUEST))
    scan=tmp_path/'scan.json';scan.write_text(json.dumps(report('CRITICAL','fixed')))
    monkeypatch.setattr(sys,'argv',['publish','--request',str(req),'--scan',str(scan),'--archive',str(tmp_path/'absent.tar')])
    aws=Mock();docker=Mock()
    monkeypatch.setattr(cli,'aws_catalog',aws)
    monkeypatch.setattr(cli.subprocess,'run',docker)
    with pytest.raises(ValueError,match='Security gate'):cli.main()
    aws.assert_not_called();docker.assert_not_called()
