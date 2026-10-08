"""Render disabled v2 workloads and narrow namespaced RBAC; never apply them."""
import argparse
import json
from pathlib import Path
from control_plane.state_v2 import runtime_image, STATE_NAME
from control_plane.kserve_effects import LANES
from control_plane.policy import DEFAULTS
from control_plane.autoscaling_v2 import DEFAULTS as HPA_DEFAULTS

NS = "production"
NAMES = dict(consumer="model-promotion-consumer-v2", processor="model-runtime-processor-v2",
             reconciler="model-champion-reconciler-v2", controller="model-release-controller-v2", router="credit-risk-router-v2")


def render(image):
    runtime_image(image)
    objects = []
    for role, name in NAMES.items():
        sa = "model-promotion-consumer" if role == "consumer" else name
        if role != "consumer":
            objects.append(dict(apiVersion="v1", kind="ServiceAccount", metadata=dict(name=sa, namespace=NS)))
        rules = [dict(apiGroups=[""], resources=["configmaps"], resourceNames=[STATE_NAME],
                      verbs=["get"] if role == "router" else ["get", "update"])]
        if role in ("controller", "router"):
            rules.append(dict(apiGroups=["serving.kserve.io"], resources=["inferenceservices"],
                              resourceNames=list(LANES.values()), verbs=["get", "update"] if role == "controller" else ["get"]))
        if role == "controller":
            # Kubernetes cannot restrict create by resourceNames. Scope is only
            # production; code restricts names and never updates ServingRuntime.
            rules += [dict(apiGroups=['apps'], resources=['deployments'],
                           resourceNames=[name + '-predictor' for name in LANES.values()], verbs=['get'])]
            rules += [dict(apiGroups=["serving.kserve.io"], resources=["servingruntimes"], verbs=["get", "create"]),
                      dict(apiGroups=["serving.kserve.io"], resources=["inferenceservices"], verbs=["create"])]
        objects += [dict(apiVersion="rbac.authorization.k8s.io/v1", kind="Role", metadata=dict(name=name, namespace=NS), rules=rules),
                    dict(apiVersion="rbac.authorization.k8s.io/v1", kind="RoleBinding", metadata=dict(name=name, namespace=NS),
                         subjects=[dict(kind="ServiceAccount", name=sa, namespace=NS)],
                         roleRef=dict(apiGroup="rbac.authorization.k8s.io", kind="Role", name=name))]
        env = dict(POD_NAMESPACE=NS, AWS_EC2_METADATA_DISABLED="true")
        if role == "controller":
            env.update(DEFAULTS)
            env.update(HPA_DEFAULTS)
            env.update(V2_PROMETHEUS_URL="http://prometheus.production.svc:9090",
                       V2_ROUTER_URL="http://credit-risk-router-v2.production.svc:8080")
        container = dict(name=role, image=image, command=["python", "-m", "control_plane.run_v2"],
                         args=[role, "--enable-live"], env=[dict(name=k, value=v) for k,v in env.items()],
                         resources=dict(requests=dict(cpu="50m", memory="64Mi"), limits=dict(memory="256Mi")),
                         securityContext=dict(allowPrivilegeEscalation=False, capabilities=dict(drop=["ALL"]), readOnlyRootFilesystem=True))
        if role == "router":
            container.update(ports=[dict(containerPort=8080)], readinessProbe=dict(httpGet=dict(path="/health/ready", port=8080)))
            objects.append(dict(apiVersion="v1", kind="Service", metadata=dict(name=name, namespace=NS),
                spec=dict(type="ClusterIP", selector={"app":name}, ports=[dict(port=8080, targetPort=8080)])))
        objects.append(dict(apiVersion="apps/v1", kind="Deployment", metadata=dict(name=name, namespace=NS),
            spec=dict(replicas=0, selector=dict(matchLabels={"app":name}),
                strategy=dict(type="Recreate"), template=dict(metadata=dict(labels={"app":name}),
                    spec=dict(serviceAccountName=sa, securityContext=dict(runAsNonRoot=True, runAsUser=10001, runAsGroup=10001,
                              seccompProfile=dict(type="RuntimeDefault")), containers=[container])))))
    return dict(apiVersion="v1", kind="List", items=objects)


def main():
    p=argparse.ArgumentParser(); p.add_argument("--image",required=True); p.add_argument("--output",type=Path,required=True)
    args=p.parse_args(); args.output.write_text(json.dumps(render(args.image),indent=2),encoding="utf-8")


if __name__ == "__main__": main()
