"""CI publication of the exact tested archive; never rebuilds or reads model S3."""
import argparse
import base64
import json
import os
import re
import subprocess
import tarfile
from pathlib import Path
from .runtime_ci import read_request, require_clean_scan, aws_catalog, publish
from control_plane.state_v2 import REPOSITORY


def archive_config(path):
    with tarfile.open(path) as tar:
        names=set(tar.getnames())
        if 'manifest.json' in names:
            rows=json.load(tar.extractfile('manifest.json'))
            if len(rows)!=1: raise ValueError('Expected one archived runtime')
            import hashlib
            return 'sha256:'+hashlib.sha256(tar.extractfile(rows[0]['Config']).read()).hexdigest()
        index=json.load(tar.extractfile('index.json'))
        def blob(digest):
            if not re.fullmatch(r'sha256:[0-9a-f]{64}',digest): raise ValueError('Invalid blob digest')
            return json.load(tar.extractfile('blobs/sha256/'+digest.split(':')[1]))
        if len(index['manifests'])!=1: raise ValueError('Expected one archived image')
        obj=blob(index['manifests'][0]['digest'])
        if 'manifests' in obj:
            images=[x for x in obj['manifests'] if x.get('platform',{}).get('architecture')=='amd64' and x.get('platform',{}).get('os')=='linux']
            if len(images)!=1: raise ValueError('Expected one linux/amd64 image')
            obj=blob(images[0]['digest'])
        return obj['config']['digest']


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('request','archive','scan'):p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args();data=read_request(a.request);report=json.loads(a.scan.read_text());require_clean_scan(report)
    if archive_config(a.archive)!=report['Metadata']['ImageID']:raise ValueError('Scanned archive identity mismatch')
    catalog=aws_catalog()
    # A publication retry never needs another build or candidate push.
    existing=catalog.lookup(data['fingerprint'])
    if existing:
        # A racing publication must still match this already-built/scanned artifact.
        verified=publish(data,existing,report,catalog)
        print(json.dumps({'status':'REUSE','image':verified}));return
    tag=os.environ['CANDIDATE_TAG']
    if not re.fullmatch(r'runtime-ci-[0-9]+-[0-9]+',tag):raise ValueError('Invalid candidate tag')
    candidate=REPOSITORY+':'+tag
    subprocess.run(['docker','load','--input',str(a.archive)],check=True)
    local=json.loads(subprocess.check_output(['docker','image','inspect','runtime-ci:candidate'],text=True))[0]
    # Docker classic identifies by config; containerd may identify by OCI index.
    if local['RootFS']['Layers']!=report['Metadata'].get('DiffIDs'):raise ValueError('Loaded layers differ from scan')
    auth=catalog.ecr.get_authorization_token(registryIds=['596432515999'])['authorizationData'][0]
    user,password=base64.b64decode(auth['authorizationToken']).decode().split(':',1)
    subprocess.run(['docker','login','--username',user,'--password-stdin',REPOSITORY.split('/')[0]],input=password,text=True,capture_output=True,check=True)
    subprocess.run(['docker','tag','runtime-ci:candidate',candidate],check=True)
    subprocess.run(['docker','push',candidate],check=True)
    rows=catalog.ecr.describe_images(registryId='596432515999',repositoryName='model-service',imageIds=[{'imageTag':tag}])['imageDetails']
    if len(rows)!=1:raise ValueError('Ambiguous registry response')
    image=REPOSITORY+'@'+rows[0]['imageDigest']
    result=publish(data,image,report,catalog)
    print(json.dumps({'status':'PUBLISHED','image':result,'fingerprint':data['fingerprint']}))


if __name__=='__main__':main()
