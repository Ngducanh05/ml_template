"""At-least-once SQS ownership transfer into durable state; no deploy privileges."""
from contextlib import contextmanager
import json
import os
import threading
import time
from prometheus_client import Counter,Gauge
from .state import enqueue,event_candidate,reconcile_candidates,REGISTRY,REGION,REPOSITORY
from .telemetry import log,serve

QUEUE=f'https://sqs.{REGION}.amazonaws.com/{REGISTRY}/mlflow-models-image-pushed'
DLQ=QUEUE+'-dlq'
QUEUE_ARN=f'arn:aws:sqs:{REGION}:{REGISTRY}:mlflow-models-image-pushed'
DLQ_ARN=QUEUE_ARN+'-dlq'
received=Counter('watcher_messages_received_total','SQS messages received')
ignored=Counter('watcher_messages_ignored_total','Durably ignored events',['reason'])
accepted=Counter('watcher_candidates_accepted_total','Durably accepted candidates')
errors=Counter('watcher_errors_total','Failed operations',['operation'])
last_success=Gauge('watcher_last_success_timestamp','Last successful SQS poll')
dlq_visible=Gauge('watcher_dlq_visible','Visible dead-letter messages requiring operator review')
reconcile_ok=Gauge('watcher_reconciliation_ready','Last reconciliation API access succeeded')

@contextmanager
def extend_visibility(sqs,receipt):
    stop=threading.Event()
    def renew():
        while not stop.wait(60):
            try: sqs.change_message_visibility(QueueUrl=QUEUE,ReceiptHandle=receipt,VisibilityTimeout=300)
            except Exception:
                errors.labels('visibility').inc();log('VISIBILITY_RENEWAL_FAILED')
    thread=threading.Thread(target=renew,daemon=True);thread.start()
    try: yield
    finally: stop.set();thread.join(timeout=1)

def handle_message(sqs,store,message):
    received.inc()
    with extend_visibility(sqs,message['ReceiptHandle']):
        candidate,reason=event_candidate(message['Body'])
        result=enqueue(store,candidate,reason,event_id=message.get('MessageId'))
        # CAS completion precedes delete. A failed delete redelivers and dedups.
        sqs.delete_message(QueueUrl=QUEUE,ReceiptHandle=message['ReceiptHandle'])
        if result=='accepted': accepted.inc()
        else: ignored.labels(result).inc()
        log('MESSAGE_PERSISTED',model=candidate['model'] if candidate else None,digest=candidate['digest'] if candidate else None,version=candidate['version'] if candidate else None,reason=result)
        return result

def reconcile(ecr,store):
    # Pagination first, followed by one shared ingestion path for the highest tag.
    details=[]
    for page in ecr.get_paginator('describe_images').paginate(registryId=REGISTRY,repositoryName=REPOSITORY,filter={'tagStatus':'TAGGED'}):
        details.extend(page.get('imageDetails',[]))
    results=[]
    for candidate in reconcile_candidates(details):
        reason=enqueue(store,candidate,event_id='ecr-reconcile')
        results.append({'candidate':candidate,'reason':reason})
        log('RECONCILE',**candidate,reason=reason)
    reconcile_ok.set(1)
    return results

def check_queues(sqs):
    attrs=sqs.get_queue_attributes(QueueUrl=QUEUE,AttributeNames=['QueueArn','MessageRetentionPeriod','RedrivePolicy'])['Attributes']
    redrive=json.loads(attrs.get('RedrivePolicy','{}'))
    if attrs.get('QueueArn')!=QUEUE_ARN: raise ValueError('Unexpected queue ARN')
    if int(attrs.get('MessageRetentionPeriod',0))!=1209600: raise ValueError('Source queue retention must be 14 days')
    if redrive.get('deadLetterTargetArn')!=DLQ_ARN or int(redrive.get('maxReceiveCount',0))!=5: raise ValueError('Source redrive must target expected DLQ after 5 receives')
    dead=sqs.get_queue_attributes(QueueUrl=DLQ,AttributeNames=['QueueArn','MessageRetentionPeriod','ApproximateNumberOfMessages'])['Attributes']
    if dead.get('QueueArn')!=DLQ_ARN or int(dead.get('MessageRetentionPeriod',0))!=1209600: raise ValueError('Unexpected DLQ ARN or retention')
    count=int(dead.get('ApproximateNumberOfMessages',0));dlq_visible.set(count)
    if count: log('DLQ_NONEMPTY',severity='ERROR',visible=count)
    return count

def main():
    import boto3
    from botocore.config import Config
    from .kube import Kube
    from .state import Store
    # Pod Identity must be present. Never silently fall back to a node role.
    if not os.environ.get('AWS_CONTAINER_CREDENTIALS_FULL_URI') or not os.environ.get('AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE'):
        raise RuntimeError('EKS Pod Identity credentials are required')
    os.environ['AWS_EC2_METADATA_DISABLED']='true'
    config=Config(connect_timeout=5,read_timeout=30,retries={'max_attempts':3,'mode':'standard'})
    sqs=boto3.client('sqs',region_name=REGION,config=config)
    ecr=boto3.client('ecr',region_name=REGION,config=config)
    store=Store(Kube());health={'last':0}
    serve(9091,lambda:time.time()-health['last']<90)
    next_reconcile=0;next_attrs=0
    while True:
        try:
            if time.time()>=next_attrs:
                check_queues(sqs);next_attrs=time.time()+60
            if time.time()>=next_reconcile:
                try: reconcile(ecr,store);next_reconcile=time.time()+3600
                except Exception as exc:
                    reconcile_ok.set(0);errors.labels('reconcile').inc()
                    # Error type/code only, never arbitrary AWS response context.
                    code=getattr(exc,'response',{}).get('Error',{}).get('Code',type(exc).__name__)
                    log('RECONCILE_BLOCKED',code=code);next_reconcile=time.time()+300
            response=sqs.receive_message(QueueUrl=QUEUE,MaxNumberOfMessages=1,WaitTimeSeconds=20,VisibilityTimeout=300,MessageSystemAttributeNames=['ApproximateReceiveCount'])
            health['last']=time.time();last_success.set(health['last'])
            log('POLL_OK',messages=len(response.get('Messages',[])))
            for message in response.get('Messages',[]):
                try: handle_message(sqs,store,message)
                except Exception as exc:
                    errors.labels('processing').inc();log('MESSAGE_RETRY',error_type=type(exc).__name__)
                    # No delete: retain native SQS retry/redrive behavior.
        except Exception as exc:
            errors.labels('poll').inc();log('POLL_ERROR',error_type=type(exc).__name__)
            time.sleep(5)

if __name__=='__main__': main()
