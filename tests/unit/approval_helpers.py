"""Synthetic native-gate completion for offline lifecycle fixtures only."""
from control_plane.authorization_v2 import authorize, evidence, REPOSITORY, WORKFLOW


def approve(store, now=0):
    state=store.read();version=str(state['highest_accepted'])
    src=dict(repository=REPOSITORY,workflow=WORKFLOW,run_id=str(100+int(version)),run_attempt='1',sha='a'*40,ref='refs/tags/v1.0.0')
    candidate=evidence(state,version,'TEST-123',src,'sha256:'+'d'*64,now)
    return authorize(store,candidate,now=now)
