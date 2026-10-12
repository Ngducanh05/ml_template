"""Transfer SQS ownership only after durable v2 ingestion; no build/deploy work."""
from .state_v2 import enqueue
from .telemetry import log

QUEUE = "https://sqs.ap-southeast-1.amazonaws.com/378457871131/mlflow-model-promoted"


def handle_message(sqs, store, message, **kwargs):
    try:
        result = enqueue(store, message["Body"], source="sqs", **kwargs)
    except (ValueError, TypeError):
        log("PROMOTION_SCHEMA_REJECTED")
        raise
    # Conflicting immutable producer identity remains on native retry/DLQ path.
    if result == "identity_conflict":
        log("PROMOTION_IDENTITY_CONFLICT")
        raise ValueError(result)
    sqs.delete_message(QueueUrl=QUEUE, ReceiptHandle=message["ReceiptHandle"])
    log("PROMOTION_DURABLY_HANDLED", outcome=result)
    return result


def poll_once(sqs, store):
    response = sqs.receive_message(QueueUrl=QUEUE, MaxNumberOfMessages=1,
                                  WaitTimeSeconds=20, VisibilityTimeout=300)
    for message in response.get("Messages", []):
        handle_message(sqs, store, message)
