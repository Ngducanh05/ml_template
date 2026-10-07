"""Explicit operator action. Enqueues only; does not start/scale a controller."""
import argparse
import json
from .state import Store,operator_retry

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--digest',required=True)
    parser.add_argument('--version',required=True,type=int)
    parser.add_argument('--operator',required=True)
    parser.add_argument('--note',required=True)
    args=parser.parse_args()
    from .kube import Kube
    result=operator_retry(Store(Kube()),args.digest,args.version,args.operator,args.note)
    print(json.dumps({'result':'queued','candidate':result}))

if __name__=='__main__': main()
