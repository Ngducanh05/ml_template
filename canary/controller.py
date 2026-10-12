"""Kubernetes lifecycle wrapper; policy remains in evaluator.py.
Wait at zero after rollback. An explicit operator setting of 10 arms a new run.
"""
import time
import evaluator

while True:
    try:
        if evaluator.api(evaluator.ROUTER + '/__admin/state')['canary_weight'] == 10:
            evaluator.run('/tmp/evaluator-events.jsonl', duration=86400)
        time.sleep(3)
    except Exception as exc:
        print('CONTROLLER_ERROR ' + str(exc), flush=True)
        try:
            evaluator.set_weight(0)
        except Exception as rollback_error:
            print('ROLLBACK_FAILED ' + str(rollback_error), flush=True)
        time.sleep(3)
