"""Explicit CI-only generic runtime preparation, catalog lookup and gated publication."""
import argparse
import json
import os
from pathlib import Path
import shutil
from control_plane.build_contract import validate_request
from control_plane.runtime_resolver import RUNTIME_CI_V2
from control_plane.runtime_catalog import Catalog

ROOT = Path(__file__).resolve().parents[1]


def read_request(path):
    raw=path.read_bytes()
    if len(raw)>65536: raise ValueError('Request too large')
    data=json.loads(raw)
    contract,profile=validate_request(data)
    if profile != RUNTIME_CI_V2 or contract.python_version != '3.12.14':
        raise ValueError('No reviewed recipe for this Python/profile')
    return data


def prepare(data, output):
    contract,profile=validate_request(data)
    if profile != RUNTIME_CI_V2 or contract.python_version != '3.12.14':
        raise ValueError('No reviewed recipe for this Python/profile')
    pins=dict(x.split('==') for x in contract.requirements)
    if not {'mlflow','scikit-learn','skops'} <= set(pins): raise ValueError('Unsupported model loader contract')
    for name,version in [('mlserver',profile.mlserver_version),('mlserver-mlflow',profile.mlserver_mlflow_version)]:
        if name in pins and pins[name]!=version: raise ValueError('Serving dependency conflict')
        pins[name]=version
    if output.exists() and any(output.iterdir()): raise ValueError('Build context must be empty')
    output.mkdir(parents=True,exist_ok=True)
    (output/'requirements.txt').write_text(''.join(f'{k}=={v}\n' for k,v in sorted(pins.items())))
    (output/'request.json').write_text(json.dumps(data,sort_keys=True))
    shutil.copyfile(ROOT/'runtime_ci/Dockerfile',output/'Dockerfile')
    # Allow only these three generated files into Docker. No model/S3/checkout.
    (output/'.dockerignore').write_text('**\n!Dockerfile\n!requirements.txt\n!request.json\n')


def require_clean_scan(report, request, waivers=None, *, now=None):
    from datetime import datetime, timezone
    from control_plane.runtime_security import evaluate_scan, load_json
    if waivers is None:
        waivers = load_json(ROOT/'security/runtime-vulnerability-waivers.json')
    decision = evaluate_scan(report, request, waivers, now=now or datetime.now(timezone.utc))
    if decision['decision'] != 'PASS':
        raise ValueError('Security gate failed: ' + ', '.join(decision['errors'] or decision['remediation'].keys()))
    return decision


def aws_catalog():
    import boto3
    # Workflow obtains temporary credentials through OIDC; never configures IAM.
    if not os.getenv('AWS_SESSION_TOKEN'): raise ValueError('Short-lived credentials required')
    return Catalog(boto3.client('ecr',region_name='ap-southeast-1'))


def verify_registry_image(image, report, catalog):
    from control_plane.state_v2 import runtime_image
    runtime_image(image)
    digest=image.split('@')[1]
    response=catalog.ecr.batch_get_image(registryId='596432515999',repositoryName='model-service',imageIds=[{'imageDigest':digest}])
    rows=response.get('images',[])
    if response.get('failures') or len(rows)!=1 or rows[0]['imageId']['imageDigest']!=digest:
        raise ValueError('Registry identity unavailable')
    manifest=json.loads(rows[0]['imageManifest'])
    if 'manifests' in manifest:
        variants=[x for x in manifest['manifests'] if x.get('platform',{}).get('os')=='linux' and x.get('platform',{}).get('architecture')=='amd64']
        if len(variants)!=1: raise ValueError('Ambiguous platform manifest')
        child=catalog.ecr.batch_get_image(registryId='596432515999',repositoryName='model-service',imageIds=[{'imageDigest':variants[0]['digest']}])
        childrows=child.get('images',[])
        if child.get('failures') or len(childrows)!=1 or childrows[0]['imageId']['imageDigest']!=variants[0]['digest']:
            raise ValueError('Platform manifest unavailable')
        manifest=json.loads(childrows[0]['imageManifest'])
    if manifest.get('config',{}).get('digest') != report['Metadata']['ImageID']:
        raise ValueError('Registry image does not match scanned image config')


def publish(data, image, report, catalog):
    validate_request(data)
    require_clean_scan(report, data)
    verify_registry_image(image, report, catalog)
    existing=catalog.lookup(data['fingerprint'])
    if existing is not None:
        if existing!=image: raise ValueError('Fingerprint digest conflict')
        return existing
    return catalog.publish(data['fingerprint'],image)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['prepare','lookup','publish']);p.add_argument('--request',type=Path,required=True)
    p.add_argument('--output',type=Path);p.add_argument('--image');p.add_argument('--scan',type=Path);a=p.parse_args();data=read_request(a.request)
    if a.command=='prepare':
        if a.output is None:p.error('--output required')
        prepare(data,a.output);print(json.dumps({'fingerprint':data['fingerprint']}));return
    catalog=aws_catalog()
    if a.command=='lookup':
        image=catalog.lookup(data['fingerprint']);result={'image':image,'build_required':image is None}
        if a.output:a.output.write_text(json.dumps(result))
        print(json.dumps(result));return
    if not a.image or not a.scan:p.error('--image and --scan required')
    print(json.dumps({'image':publish(data,a.image,json.loads(a.scan.read_text()),catalog)}))


if __name__=='__main__': main()
