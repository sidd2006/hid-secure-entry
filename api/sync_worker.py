"""Background sync: outbox rows -> S3 events/<event_id>.json (us-east-1).

- Runs every 5 s, only while ONLINE. Also refreshes the mode cache.
- Bucket from env S3_SYNC_BUCKET, else hid-kiosk-events-<account id>;
  created if missing.
- A row is marked synced only after PutObject returns HTTP 200. Retries reuse
  the same key, so re-sending an event overwrites, never duplicates.
- Never changes a past decision; only the `synced` flag is written.
"""
import asyncio
import json
import logging
import os

from . import mode, outbox

log = logging.getLogger("uvicorn.error")
INTERVAL_S = 5.0
REGION = "us-east-1"


class S3Uploader:
    def __init__(self, client=None, bucket: str | None = None) -> None:
        self._client = client
        self._bucket = bucket
        self._ensured = False

    @property
    def client(self):
        if self._client is None:
            import boto3
            from botocore.config import Config
            self._client = boto3.client("s3", region_name=REGION, config=Config(
                connect_timeout=2, read_timeout=5, retries={"max_attempts": 2},
                tcp_keepalive=True))
        return self._client

    @property
    def bucket(self) -> str:
        if self._bucket is None:
            self._bucket = os.environ.get("S3_SYNC_BUCKET") or self._default_bucket()
        return self._bucket

    @staticmethod
    def _default_bucket() -> str:
        import boto3
        acct = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
        return f"hid-kiosk-events-{acct}"

    def ensure_bucket(self) -> None:
        if self._ensured:
            return
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception as e:
            if getattr(e, "response", {}).get("Error", {}).get("Code") not in ("404", "NoSuchBucket"):
                raise
            self.client.create_bucket(Bucket=self.bucket)  # us-east-1: no LocationConstraint
            log.info("sync: created bucket for events")
        self._ensured = True

    def put(self, row: dict) -> bool:
        resp = self.client.put_object(
            Bucket=self.bucket, Key=f"events/{row['event_id']}.json",
            Body=json.dumps(outbox.event_payload(row), separators=(",", ":")).encode("utf-8"),
            ContentType="application/json")
        return resp.get("ResponseMetadata", {}).get("HTTPStatusCode") == 200


def sync_once(box: outbox.Outbox, uploader: S3Uploader, limit: int = 100) -> dict:
    """Upload unsynced rows. -> {"sent": n, "failed": n}. Never raises."""
    sent = failed = 0
    try:
        uploader.ensure_bucket()
    except Exception as e:
        log.warning("sync: bucket unavailable (%s)", type(e).__name__)
        return {"sent": 0, "failed": len(box.unsynced(limit))}
    for row in box.unsynced(limit):
        try:
            ok = uploader.put(row)
        except Exception as e:
            log.warning("sync: put failed for %s (%s)", row["event_id"], type(e).__name__)
            ok = False
        if ok:
            box.mark_synced(row["event_id"])
            sent += 1
        else:
            failed += 1
    return {"sent": sent, "failed": failed}


async def run_forever(stop: asyncio.Event, uploader: S3Uploader | None = None) -> None:
    uploader = uploader or S3Uploader()
    while not stop.is_set():
        try:
            m = await asyncio.to_thread(mode.refresh)
            if m == "ONLINE" and os.environ.get("S3_SYNC_ENABLED", "").lower() in {'1', 'true'}:
                res = await asyncio.to_thread(sync_once, outbox.get(), uploader)
                if res["sent"] or res["failed"]:
                    log.info("sync: sent=%d failed=%d", res["sent"], res["failed"])
        except Exception:
            log.exception("sync loop error")
        try:
            await asyncio.wait_for(stop.wait(), timeout=INTERVAL_S)
        except asyncio.TimeoutError:
            pass
