"""Read real V2 router observations; undefined samples remain NaN policy inputs."""
import math
from urllib.parse import urlencode
from .kserve_effects import read_json


class Observations:
    def __init__(self, prometheus, router, get=read_json):
        self.prometheus, self.router_url, self.get = prometheus.rstrip("/"), router.rstrip("/"), get

    def router(self):
        return self.get(self.router_url + "/state")

    def metrics(self):
        labels = 'job="credit-risk-router-v2",track="canary"'
        requests = "credit_risk_router_requests_total"
        total = f'sum(rate({requests}{{{labels}}}[30s]))'
        queries = {
            "up": 'min(up{job="credit-risk-router-v2"})',
            "age": 'max(time()-timestamp(up{job="credit-risk-router-v2"}))',
            "traffic": f'sum(increase({requests}{{{labels}}}[30s]))',
            "success": f'sum(increase({requests}{{{labels},status=~"2.."}}[30s]))',
            "errors": f'sum(rate({requests}{{{labels},status=~"5.."}}[30s])) / clamp_min({total},1e-12)',
            "p95": f'histogram_quantile(0.95,sum by(le)(rate(credit_risk_router_request_duration_seconds_bucket{{{labels}}}[30s])))',
            "stable_errors": f'sum(rate({requests}{{job="credit-risk-router-v2",track="stable",status=~"5.."}}[30s])) / clamp_min(sum(rate({requests}{{job="credit-risk-router-v2",track="stable"}}[30s])),1e-12)',
        }
        values = {}
        for key, query in queries.items():
            response = self.get(self.prometheus + "/api/v1/query?" + urlencode({"query": query}))
            rows = response.get("data", {}).get("result", [])
            value = float("nan")
            if response.get("status") == "success" and len(rows) == 1:
                try: value = float(rows[0]["value"][1])
                except (KeyError, IndexError, ValueError, TypeError): pass
            values[key] = value
        return values
