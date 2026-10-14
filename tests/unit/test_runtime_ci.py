import copy
import json
from pathlib import Path
from unittest.mock import Mock
import pytest
from control_plane.build_contract import build_request, validate_request
from control_plane.runtime_resolver import ArtifactContract, RUNTIME_CI_V2, normalize_requirements, runtime_fingerprint
from scripts.runtime_ci import prepare, require_clean_scan, publish
from control_plane.state_v2 import REPOSITORY

CONTRACT=ArtifactContract('3.12.14',normalize_requirements('mlflow==3.14.0\nskops==0.16.0\nscikit-learn==1.7.2'))
REQUEST=build_request(CONTRACT,RUNTIME_CI_V2)
IMAGE=REPOSITORY+'@sha256:'+'a'*64
CONFIG='sha256:'+'b'*64


def scan(vulns=None):
    return {'ArtifactType':'container_image','Metadata':{'ImageID':CONFIG},'Results':[{'Target':'runtime','Class':'lang-pkgs','Type':'python-pkg','Vulnerabilities':vulns or []}]}


def test_independent_fingerprint_and_context_excludes_model(tmp_path):
    assert validate_request(REQUEST)[1]==RUNTIME_CI_V2
    prepare(REQUEST,tmp_path)
    assert {x.name for x in tmp_path.iterdir()}=={'Dockerfile','.dockerignore','requirements.txt','request.json'}
    assert 'mlserver==1.7.1' in (tmp_path/'requirements.txt').read_text()
    bad=copy.deepcopy(REQUEST);bad['fingerprint']='sha256:'+'0'*64
    with pytest.raises(ValueError):prepare(bad,tmp_path/'bad')
    bad=copy.deepcopy(REQUEST);bad['artifact_uri']='s3://not-allowed/model'
    with pytest.raises(ValueError):validate_request(bad)
    with pytest.raises(ValueError,match='empty'):prepare(REQUEST,tmp_path)


def test_unreviewed_python_profile_fails_without_build(tmp_path):
    with pytest.raises(ValueError,match='recipe'):prepare(build_request(CONTRACT),tmp_path)
    other=ArtifactContract('3.11.14',CONTRACT.requirements)
    with pytest.raises(ValueError,match='recipe'):prepare(build_request(other,RUNTIME_CI_V2),tmp_path)


@pytest.mark.parametrize('report',[{}, {'ArtifactType':'container_image','Metadata':{'ImageID':CONFIG},'Results':[]},scan([{'Severity':'HIGH'}]),scan([{'Severity':'CRITICAL'}]),scan([{'Severity':'UNKNOWN'}])])
def test_security_failure_never_publishes(report):
    c=Mock()
    with pytest.raises(ValueError):publish(REQUEST,IMAGE,report,c)
    c.publish.assert_not_called()


def test_registry_config_must_match_scan_and_collision_fails():
    c=Mock();c.ecr.batch_get_image.return_value={'images':[{'imageId':{'imageDigest':IMAGE.split('@')[1]},'imageManifest':json.dumps({'config':{'digest':CONFIG}})}]}
    c.lookup.return_value=None;c.publish.return_value=IMAGE
    assert publish(REQUEST,IMAGE,scan(),c)==IMAGE
    c.lookup.return_value=REPOSITORY+'@sha256:'+'c'*64
    with pytest.raises(ValueError,match='conflict'):publish(REQUEST,IMAGE,scan(),c)
    c.ecr.batch_get_image.return_value['images'][0]['imageManifest']=json.dumps({'config':{'digest':'different'}})
    with pytest.raises(ValueError,match='scanned'):publish(REQUEST,IMAGE,scan(),c)


def test_processor_legacy_hit_is_preserved_and_miss_uses_versioned_recipe():
    from control_plane.processor_v2 import Processor
    from control_plane.state_v2 import Store,enqueue
    from test_state_v2 import Backend,event
    legacy=runtime_fingerprint(CONTRACT);new=REQUEST['fingerprint'];assert legacy!=new
    source=Mock();source.inspect.return_value=CONTRACT
    for lookup,expected,fp in [(lambda f:IMAGE,'READY_FOR_RELEASE',legacy),(lambda f:None,'BUILD_REQUIRED',new),(lambda f:IMAGE if f==new else None,'READY_FOR_RELEASE',new)]:
        store=Store(Backend());enqueue(store,event());catalog=Mock();catalog.lookup.side_effect=lookup
        assert Processor(store,source,catalog,clock=lambda:0).tick()==expected
        assert store.read()['pending'][0]['fingerprint']==fp
        if fp==legacy:assert catalog.lookup.call_count==1


def test_build_job_has_no_oidc_or_aws_identity():
    import yaml
    doc=yaml.load(Path('.github/workflows/runtime-build.yml').read_text(),Loader=yaml.BaseLoader)
    build=doc['jobs']['build'];assert build['permissions']=={'contents':'read'}
    assert 'configure-aws-credentials' not in json.dumps(build)
    assert 'needs.catalog.outputs.build_required' in build['if']
    assert doc['jobs']['publish']['environment']=='runtime-build-publish'
    assert 'environment' not in doc['jobs']['catalog']
    assert 'RELEASE_CATALOG_READ_ROLE_ARN' in json.dumps(doc['jobs']['catalog'])
    assert 'RUNTIME_BUILD_ROLE_ARN' not in json.dumps(doc['jobs']['catalog'])


def test_archive_config_identity_is_derived_from_bytes(tmp_path):
    import hashlib,io,tarfile
    from scripts.runtime_ci_publish import archive_config
    config=b'{"rootfs":{"diff_ids":["example"]}}'
    path=tmp_path/'runtime.tar'
    with tarfile.open(path,'w') as tar:
        for name,data in [('manifest.json',json.dumps([{'Config':'config.json'}]).encode()),('config.json',config)]:
            info=tarfile.TarInfo(name);info.size=len(data);tar.addfile(info,io.BytesIO(data))
    assert archive_config(path)=='sha256:'+hashlib.sha256(config).hexdigest()


def test_oci_index_registry_scan_identity():
    c=Mock();child='sha256:'+'d'*64
    def response(digest,manifest):return {'images':[{'imageId':{'imageDigest':digest},'imageManifest':json.dumps(manifest)}]}
    c.ecr.batch_get_image.side_effect=[response(IMAGE.split('@')[1],{'manifests':[{'digest':child,'platform':{'os':'linux','architecture':'amd64'}}]}),response(child,{'config':{'digest':CONFIG}})]
    c.lookup.return_value=None;c.publish.return_value=IMAGE
    assert publish(REQUEST,IMAGE,scan(),c)==IMAGE
