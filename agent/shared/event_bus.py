import json
from typing import AsyncIterator

from redis.asyncio import Redis

from agent.shared.models import InfraEvent

STREAM_MAX_LEN = 10_000


async def publish(redis: Redis, event: InfraEvent) -> str:
    stream_key = f"events:{event.domain}"
    raw_id = await redis.xadd(
        stream_key,
        {
            "source": event.source,
            "event_type": event.event_type,
            "severity": event.severity,
            "resource_id": event.resource_id,
            "timestamp": event.timestamp.isoformat(),
            "payload": json.dumps(event.raw_payload),
            "labels": json.dumps(event.labels),
        },
        maxlen=STREAM_MAX_LEN,
        approximate=True,
    )
    event.stream_id = raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id)
    return event.stream_id


async def tail(redis: Redis, domain: str) -> AsyncIterator[dict]:
    """Yield decoded event dicts from a domain stream, starting from now."""
    last_id = b"$"
    while True:
        entries = await redis.xread({f"events:{domain}": last_id}, count=20, block=1000)
        for _, messages in entries:
            for msg_id, fields in messages:
                last_id = msg_id
                yield {
                    "stream_id": msg_id.decode(),
                    "source": fields[b"source"].decode(),
                    "event_type": fields[b"event_type"].decode(),
                    "severity": fields[b"severity"].decode(),
                    "resource_id": fields[b"resource_id"].decode(),
                    "timestamp": fields[b"timestamp"].decode(),
                    "payload": json.loads(fields[b"payload"]),
                    "labels": json.loads(fields[b"labels"]),
                }
