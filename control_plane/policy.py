"""Validated release policy shared by runtime and deployment rendering."""
from dataclasses import dataclass
import math
import os

DEFAULTS = {'MIN_CANARY_REQUESTS':'1', 'CANARY_DWELL_SECONDS':'35',
            'RELEASE_STAGE_TIMEOUT_SECONDS':'300', 'MAX_ERROR_RATIO':'0.05',
            'MAX_P95_SECONDS':'0.1'}

@dataclass(frozen=True)
class Policy:
    minimum: int
    dwell: float
    timeout: float
    error_limit: float
    p95_limit: float

    @classmethod
    def from_env(cls, env=None):
        values={**DEFAULTS, **(os.environ if env is None else env)}
        p=cls(int(values['MIN_CANARY_REQUESTS']),float(values['CANARY_DWELL_SECONDS']),
              float(values['RELEASE_STAGE_TIMEOUT_SECONDS']),float(values['MAX_ERROR_RATIO']),
              float(values['MAX_P95_SECONDS']))
        if (p.minimum<1 or not all(math.isfinite(x) for x in (p.dwell,p.timeout,p.error_limit,p.p95_limit))
                or p.dwell<0 or p.timeout<=p.dwell or not 0<p.error_limit<=1 or p.p95_limit<=0):
            raise ValueError('Invalid release policy')
        return p
