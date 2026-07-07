"""
Lambda handler for S3 ObjectCreated events — generates WebP thumbnails.

When a file lands in the drive bucket, this creates a small WebP thumbnail
at thumbs/{original_key}.webp (S3 STANDARD class) so the grid never has to
download multi-MB originals — and never pays Glacier IR retrieval fees.

Guards against self-triggering: writes to thumbs/ fire another ObjectCreated
event, but the prefix check below returns immediately, so no recursion.
"""
import io
import os
from urllib.parse import unquote_plus

import boto3
from PIL import Image, ImageOps

MAX_DIM        = 400          # bounding box for thumbnails
WEBP_QUALITY   = 80
MAX_SOURCE_MB  = 80           # skip anything bigger — not worth thumbnailing
SKIP_PREFIXES  = ("thumbs/", "temp-zips/", "static/")

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif")


def handler(event, context):
    for record in event.get("Records", []):
        try:
            _process(record)
        except Exception as e:
            # Log and continue — raising would make S3/Lambda retry and
            # regenerate the same thumbnail up to 3 times.
            key = record.get("s3", {}).get("object", {}).get("key", "?")
            print(f"[ERROR] thumbnailer failed for key={key}: {e}")


def _process(record):
    bucket = record["s3"]["bucket"]["name"]
    key = unquote_plus(record["s3"]["object"]["key"])

    if key.startswith(SKIP_PREFIXES):
        return

    region = os.environ.get("AWS_REGION", "ap-southeast-2")
    s3 = boto3.client("s3", region_name=region)
    thumb_key = f"thumbs/{key}.webp"

    # Already generated (e.g. this is the GLACIER_IR copy event after upload)
    try:
        s3.head_object(Bucket=bucket, Key=thumb_key)
        return
    except s3.exceptions.ClientError:
        pass

    head = s3.head_object(Bucket=bucket, Key=key)
    content_type = head.get("ContentType", "")
    if not (content_type.startswith("image/") or key.lower().endswith(IMAGE_EXTENSIONS)):
        return
    if head["ContentLength"] > MAX_SOURCE_MB * 1024 * 1024:
        return

    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()

    img = Image.open(io.BytesIO(body))
    img = ImageOps.exif_transpose(img)  # respect phone camera orientation
    if img.mode in ("P", "PA", "LA"):
        img = img.convert("RGBA")
    elif img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGB")
    img.thumbnail((MAX_DIM, MAX_DIM))

    out = io.BytesIO()
    img.save(out, format="WEBP", quality=WEBP_QUALITY, method=4)
    out.seek(0)

    s3.put_object(
        Bucket=bucket,
        Key=thumb_key,
        Body=out.getvalue(),
        ContentType="image/webp",
        CacheControl="public, max-age=31536000, immutable",
    )
    print(f"[OK] thumb {thumb_key} ({len(body)} -> {out.getbuffer().nbytes} bytes)")
