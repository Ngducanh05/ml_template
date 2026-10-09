"""Release-only Kubernetes backend; static scoped credentials, GET/PUT one v2 CM."""
import base64
import copy
import json
import ssl
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from .state_v2 import STATE_NAME, Store
from .state import Conflict


class AuthorizationBackend:
    def __init__(self, config, *, writable=False):
        # Dedicated minimal JSON credential. No kubeconfig exec plugins or ambient identity.
        if set(config) != {'server','certificate_authority_data','token','namespace','state_name'}:
            raise ValueError('Unexpected release credential fields')
        if config['namespace'] != 'production' or config['state_name'] != STATE_NAME:
            raise ValueError('Legacy or other state access prohibited')
        from urllib.parse import urlsplit
        u=urlsplit(config['server'])
        if u.scheme!='https' or not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in ('','/'):
            raise ValueError('TLS Kubernetes endpoint required')
        self.url=config['server'].rstrip('/')+'/api/v1/namespaces/production/configmaps/'+STATE_NAME
        self.context=ssl.create_default_context(cadata=base64.b64decode(config['certificate_authority_data'],validate=True).decode())
        self.token=config['token'];self.writable=writable;self.original=None
        if not isinstance(self.token,str) or not self.token.strip():raise ValueError('Missing scoped token')

    def request(self,method,data=None):
        if method not in ('GET','PUT') or (method=='PUT' and not self.writable):raise ValueError('Read-only release identity')
        req=Request(self.url,method=method,data=None if data is None else json.dumps(data).encode(),
            headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        try:
            with urlopen(req,context=self.context,timeout=15) as r:return json.load(r)
        except HTTPError as exc:
            if exc.code==409:raise Conflict() from None
            raise RuntimeError('Release state API failed: HTTP '+str(exc.code)) from None

    def read_state(self):
        cm=self.request('GET')
        if cm['metadata']['name']!=STATE_NAME or cm['metadata']['namespace']!='production':raise ValueError('Wrong state resource')
        state=json.loads(cm['data']['state.json']);Store.validate(state);self.original=cm
        return cm['metadata']['resourceVersion'],state

    def write_state(self,revision,state):
        if not self.writable:raise ValueError('Read-only release identity')
        if not self.original or self.original['metadata']['resourceVersion']!=revision:raise Conflict()
        # Preserve metadata and unrelated data; only existing state.json is replaced.
        cm=copy.deepcopy(self.original);cm['data']['state.json']=json.dumps(state,separators=(',',':'))
        self.request('PUT',cm)
