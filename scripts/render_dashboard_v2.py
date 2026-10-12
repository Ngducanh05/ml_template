"""Render dashboard/exporter objects offline; reject unresolved image identities."""
import argparse
import json
from pathlib import Path
import yaml
from control_plane.state_v2 import runtime_image

ROOT=Path(__file__).resolve().parents[1]


def render(image):
    runtime_image(image)
    root=ROOT/'k8s/monitoring'
    files=['grafana/dashboard-mlops-v2-infra.configmap.yaml','servicemonitor-router-v2.yaml','servicemonitor-state-exporter.yaml','state-exporter/rbac.yaml','state-exporter/deployment-service.yaml']
    items=[]
    for file in files:
        items.extend(x for x in yaml.safe_load_all((root/file).read_text(encoding='utf-8').replace('IMAGE_DIGEST_REQUIRED',image)) if x)
    return {'apiVersion':'v1','kind':'List','items':items}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--image',required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(render(args.image),indent=2),encoding='utf-8')

if __name__=='__main__':main()
