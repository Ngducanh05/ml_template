"""Render one idempotently named read-only E2E Job; never applies resources."""
import argparse
import hashlib
import json
from pathlib import Path
import re
from .runner import validate_config


def render(config,payload,image):
    validate_config(config)
    if not re.fullmatch(r"[a-zA-Z0-9./_-]+@sha256:[0-9a-f]{64}",image): raise ValueError("Immutable image required")
    source=Path(__file__).with_name("runner.py").read_text(encoding="utf-8")
    identity=hashlib.sha256(json.dumps(dict(config=config,payload=payload,image=image,source=source),sort_keys=True).encode()).hexdigest()
    # Stable name for a run ID: callers reject a different identity at this name.
    name="v2-e2e-"+config["run_id"]
    ns=config["namespace"]
    meta=dict(name=name,namespace=ns,labels={"app.kubernetes.io/name":"v2-e2e-runner"},annotations={"ml-template.io/e2e-identity":identity,"ml-template.io/synthetic-policy-test":str(config["mode"]=="policy-test").lower()})
    objects=[dict(apiVersion="v1",kind="ServiceAccount",metadata=meta),
        dict(apiVersion="rbac.authorization.k8s.io/v1",kind="Role",metadata=meta,rules=[dict(apiGroups=[""],resources=["configmaps"],resourceNames=["model-release-state-v2"],verbs=["get"]), dict(apiGroups=["apps"],resources=["deployments"],resourceNames=["credit-risk-stable-v2-predictor","credit-risk-canary-v2-predictor"],verbs=["get"]) ]),
        dict(apiVersion="rbac.authorization.k8s.io/v1",kind="RoleBinding",metadata=meta,roleRef=dict(apiGroup="rbac.authorization.k8s.io",kind="Role",name=name),subjects=[dict(kind="ServiceAccount",name=name,namespace=ns)]),
        dict(apiVersion="v1",kind="ConfigMap",metadata=meta,immutable=True,data={"runner.py":source,"config.json":json.dumps(config),"payload.json":json.dumps(payload)})]
    objects.append(dict(apiVersion="batch/v1",kind="Job",metadata=meta,spec=dict(backoffLimit=0,activeDeadlineSeconds=config["duration"]+30,ttlSecondsAfterFinished=86400,
        template=dict(metadata=dict(labels={"app.kubernetes.io/name":"v2-e2e-runner"}),spec=dict(serviceAccountName=name,restartPolicy="Never",
            securityContext=dict(runAsNonRoot=True,runAsUser=10001,runAsGroup=10001,fsGroup=10001,seccompProfile=dict(type="RuntimeDefault")),
            containers=[dict(name="runner",image=image,command=["python","/runner/runner.py","--config","/runner/config.json","--payload","/runner/payload.json"],
                env=[dict(name="AWS_EC2_METADATA_DISABLED",value="true")],
                resources=dict(requests=dict(cpu="50m",memory="64Mi"),limits=dict(cpu="500m",memory="256Mi")),
                securityContext=dict(allowPrivilegeEscalation=False,readOnlyRootFilesystem=True,capabilities=dict(drop=["ALL"])),
                volumeMounts=[dict(name="runner",mountPath="/runner",readOnly=True),dict(name="evidence",mountPath="/evidence")])],
            volumes=[dict(name="runner",configMap=dict(name=name)),dict(name="evidence",emptyDir=dict(sizeLimit="32Mi"))])))))
    return dict(apiVersion="v1",kind="List",items=objects)


def main():
    p=argparse.ArgumentParser(); p.add_argument("--config",type=Path,required=True);p.add_argument("--payload",type=Path,required=True)
    p.add_argument("--image",required=True);p.add_argument("--output",type=Path,required=True)
    args=p.parse_args();args.output.write_text(json.dumps(render(json.loads(args.config.read_text()),json.loads(args.payload.read_text()),args.image),indent=2),encoding="utf-8")


if __name__=="__main__":main()
