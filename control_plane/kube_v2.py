"""Kubernetes CAS adapter restricted to the isolated v2 resource names."""
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from .kube import Kube
from .state import Conflict
from .state_v2 import STATE_NAME, Store
from .kserve_effects import LANES


class KubeV2(Kube):
    def __init__(self):
        super().__init__()
        if self.ns != "production":
            raise ValueError("Unexpected v2 namespace")
        self.name = STATE_NAME

    def request(self, path, method="GET", data=None):
        headers = {"Authorization": "Bearer " + self.token.read_text().strip(), "Content-Type": "application/json"}
        request = Request(self.base + path, method=method, headers=headers,
                          data=None if data is None else json.dumps(data).encode())
        try:
            with urlopen(request, context=self.context, timeout=8) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code == 409:
                raise Conflict() from None
            if exc.code == 404 and method == "GET":
                return None
            raise RuntimeError(f"Kubernetes {method}: HTTP {exc.code}") from None

    def read_state(self):
        cm = self.request(f"/api/v1/namespaces/{self.ns}/configmaps/{STATE_NAME}")
        if cm is None:
            raise RuntimeError("Isolated v2 state must be initialized explicitly")
        state = json.loads(cm["data"]["state.json"])
        Store.validate(state)
        return cm["metadata"]["resourceVersion"], state

    def path(self, kind, name=None):
        if kind not in ("servingruntimes", "inferenceservices"):
            raise ValueError("Resource not allowed")
        if name and not (name in LANES.values() if kind == "inferenceservices" else name.startswith("runtime-")):
            raise ValueError("Legacy resource access prohibited")
        version = "v1alpha1" if kind == "servingruntimes" else "v1beta1"
        return f"/apis/serving.kserve.io/{version}/namespaces/{self.ns}/{kind}" + ("/" + name if name else "")

    def get_deployment(self, lane):
        name = LANES[lane] + '-predictor'
        return self.request(f'/apis/apps/v1/namespaces/{self.ns}/deployments/{name}')

    def get(self, kind, name):
        return self.request(self.path(kind, name))

    def create(self, kind, obj):
        self.path(kind, obj["metadata"]["name"])
        return self.request(self.path(kind), "POST", obj)

    def replace(self, kind, obj):
        if kind == "servingruntimes":
            raise ValueError("Runtime identities cannot be replaced")
        return self.request(self.path(kind, obj["metadata"]["name"]), "PUT", obj)
