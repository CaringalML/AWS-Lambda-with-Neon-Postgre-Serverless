"""
Lambda handler for S3 ObjectCreated events — generates WebP derivatives.

When a file lands in the drive bucket, this creates:
  thumbs/{key}.webp    ~400px  — grid/tile view
  previews/{key}.webp  ~1600px — lightbox preview
both in S3 STANDARD class, so the UI never downloads multi-MB originals —
and never pays Glacier IR retrieval fees. Originals are only fetched on
explicit download.

Permanent failures (undecodable file, oversized, original in Deep Archive)
write a tiny placeholder derivative tagged with the "nova-placeholder"
metadata key. The head-check in the app sees it and stops re-invoking the
backfill for that file. Transient errors write nothing, so they retry on a
later browse. notify.py deletes placeholders when a Glacier restore
completes, letting real derivatives regenerate.

Guards against self-triggering: writes to thumbs//previews/ fire another
ObjectCreated event, but the prefix check below returns immediately.
"""
import io
import os
from urllib.parse import unquote_plus

import boto3
from botocore.exceptions import ClientError
from PIL import Image, ImageOps

# (key prefix, bounding box, webp quality)
DERIVATIVES = (
    ("thumbs/", 400, 80),
    ("previews/", 1600, 82),
)
MAX_SOURCE_MB = 80            # skip anything bigger — not worth processing
SKIP_PREFIXES = ("thumbs/", "previews/", "temp-zips/", "static/")
PLACEHOLDER_META = "nova-placeholder"

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif")


def handler(event, context):
    for record in event.get("Records", []):
        try:
            _process(record)
        except Exception as e:
            # Log and continue — raising would make S3/Lambda retry and
            # regenerate the same derivatives up to 3 times.
            key = record.get("s3", {}).get("object", {}).get("key", "?")
            print(f"[ERROR] thumbnailer failed for key={key}: {e}")


def _write_placeholders(s3, bucket, missing, reason):
    """Mark a file as permanently un-thumbnailable so the app's head-check
    succeeds and the backfill stops re-invoking this Lambda for it."""
    img = Image.new("RGB", (1, 1), (19, 21, 28))
    out = io.BytesIO()
    img.save(out, format="WEBP", quality=50)
    body = out.getvalue()
    for out_key, _, _ in missing:
        s3.put_object(
            Bucket=bucket,
            Key=out_key,
            Body=body,
            ContentType="image/webp",
            Metadata={PLACEHOLDER_META: reason},
            CacheControl="no-store",
        )
    print(f"[PLACEHOLDER] {missing[0][0] if missing else '?'} reason={reason}")


def _process(record):
    bucket = record["s3"]["bucket"]["name"]
    key = unquote_plus(record["s3"]["object"]["key"])

    if key.startswith(SKIP_PREFIXES):
        return

    region = os.environ.get("AWS_REGION", "ap-southeast-2")
    s3 = boto3.client("s3", region_name=region)

    # Which derivatives are missing? (the GLACIER_IR copy event after upload,
    # and backfill re-invokes, land here with everything already generated)
    missing = []
    for prefix, dim, quality in DERIVATIVES:
        out_key = f"{prefix}{key}.webp"
        try:
            s3.head_object(Bucket=bucket, Key=out_key)
        except ClientError:
            missing.append((out_key, dim, quality))
    if not missing:
        return

    head = s3.head_object(Bucket=bucket, Key=key)
    content_type = head.get("ContentType", "")
    if not (content_type.startswith("image/") or key.lower().endswith(IMAGE_EXTENSIONS)):
        _write_placeholders(s3, bucket, missing, "not-image")
        return
    if head["ContentLength"] > MAX_SOURCE_MB * 1024 * 1024:
        _write_placeholders(s3, bucket, missing, "too-large")
        return

    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as e:
        if e.response["Error"]["Code"] == "InvalidObjectState":
            # Original is in Deep Archive and not restored — placeholder is
            # cleaned up by notify.py when the restore completes.
            _write_placeholders(s3, bucket, missing, "archived")
            return
        raise  # transient S3 error — no placeholder, retry on next browse

    try:
        base = Image.open(io.BytesIO(body))
        base = ImageOps.exif_transpose(base)  # respect phone camera orientation
        if base.mode in ("P", "PA", "LA"):
            base = base.convert("RGBA")
        elif base.mode not in ("RGB", "RGBA", "L"):
            base = base.convert("RGB")
    except Exception:
        _write_placeholders(s3, bucket, missing, "undecodable")
        return

    for out_key, dim, quality in missing:
        img = base.copy()
        img.thumbnail((dim, dim))
        out = io.BytesIO()
        img.save(out, format="WEBP", quality=quality, method=4)
        s3.put_object(
            Bucket=bucket,
            Key=out_key,
            Body=out.getvalue(),
            ContentType="image/webp",
            CacheControl="public, max-age=31536000, immutable",
        )
        print(f"[OK] {out_key} ({len(body)} -> {out.getbuffer().nbytes} bytes)")
