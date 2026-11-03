"""Sends order confirmation emails in batches. Release 2026.10.02-2."""
BATCH_SIZE = 500          # the provider allows 100 messages for each request
SEND_INTERVAL_SECONDS = 1


def send_pending(queue, provider, log):
    while batch := queue.take(BATCH_SIZE):
        response = provider.send_many(batch)
        if response.status == 429:
            log.warning("[email-batch] throttled status=429 size=%d", len(batch))
            queue.put_back(batch)
