"""
Lambda handler for S3 ObjectCreated events — generates WebP derivatives.

When a file lands in the drive bucket, this creates:
  thumbs/{key}.webp    ~400px  — grid/tile view
  previews/{key}.webp  ~1600px — lightbox preview
both in S3 STANDARD class, so the UI never downloads multi-MB originals —
and never pays Glacier IR retrieval fees. Originals are only fetched on
explicit download.

Guards against self-triggering: writes to thumbs//previews/ fire another
ObjectCreated event, but the prefix check below returns immediately.
"""
import io
import os
from urllib.parse import unquote_plus

import boto3
from PIL import Image, ImageOps

# (key prefix, bounding box, webp quality)
DERIVATIVES = (
    ("thumbs/", 400, 80),
    ("previews/", 1600, 82),
)
MAX_SOURCE_MB = 80            # skip anything bigger — not worth processing
SKIP_PREFIXES = ("thumbs/", "previews/", "temp-zips/", "static/")

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
        except s3.exceptions.ClientError:
            missing.append((out_key, dim, quality))
    if not missing:
        return

    head = s3.head_object(Bucket=bucket, Key=key)
    content_type = head.get("ContentType", "")
    if not (content_type.startswith("image/") or key.lower().endswith(IMAGE_EXTENSIONS)):
        return
    if head["ContentLength"] > MAX_SOURCE_MB * 1024 * 1024:
        return

    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()

    base = Image.open(io.BytesIO(body))
    base = ImageOps.exif_transpose(base)  # respect phone camera orientation
    if base.mode in ("P", "PA", "LA"):
        base = base.convert("RGBA")
    elif base.mode not in ("RGB", "RGBA", "L"):
        base = base.convert("RGB")

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
