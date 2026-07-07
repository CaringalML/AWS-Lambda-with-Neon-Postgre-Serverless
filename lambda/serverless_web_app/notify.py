"""
Lambda handler for S3 ObjectRestore:Completed events.

Triggered by S3 → Lambda notification when a Glacier restore finishes.
Looks up the file in DynamoDB, sends a "ready to download" email via Resend,
and updates the restore_status to 'ready'.
"""
import json
import os
from datetime import datetime, timezone, timedelta
from urllib.parse import unquote_plus

import boto3
from boto3.dynamodb.conditions import Key
import resend


def handler(event, context):
    for record in event.get("Records", []):
        event_name = record.get("eventName", "")
        if "ObjectRestore:Completed" not in event_name:
            continue

        s3_key = unquote_plus(record["s3"]["object"]["key"])
        try:
            _handle_restore_completed(s3_key)
        except Exception as e:
            print(f"[ERROR] notify handler failed for key={s3_key}: {e}")


def _handle_restore_completed(s3_key):
    region     = os.environ.get("AWS_REGION", "ap-southeast-2")
    table_name = os.environ["DYNAMODB_FILES_TABLE"]

    ddb   = boto3.resource("dynamodb", region_name=region)
    table = ddb.Table(table_name)

    resp  = table.query(
        IndexName="s3key-index",
        KeyConditionExpression=Key("s3_key").eq(s3_key),
        Limit=1,
    )
    items = resp.get("Items", [])
    if not items:
        return

    item         = items[0]
    file_id      = item["file_id"]
    file_name    = item.get("name", "")
    notify_email = item.get("restore_notify_email", "")
    restore_status = item.get("restore_status", "")

    if restore_status == "ready":
        return

    expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    table.update_item(
        Key={"file_id": file_id},
        UpdateExpression="SET restore_status = :rs, restore_expires_at = :re",
        ExpressionAttributeValues={":rs": "ready", ":re": expires_at},
    )

    if not notify_email:
        return

    ssm = boto3.client("ssm", region_name=region)
    resend_key = ssm.get_parameter(
        Name=os.environ["SSM_RESEND_API_KEY_NAME"], WithDecryption=True
    )["Parameter"]["Value"]

    resend.api_key = resend_key
    from_email     = os.environ.get("DRIVE_FROM_EMAIL", "noreply@nodepulsecaringal.xyz")

    resend.Emails.send({
        "from":    from_email,
        "to":      [notify_email],
        "subject": f'NovaDrive: "{file_name}" is ready to download',
        "html":    _build_ready_email(file_name),
    })


def _build_ready_email(file_name):
    drive_url = os.environ.get("DRIVE_URL", "https://drive.nodepulsecaringal.xyz/drive/")
    return f"""
    <div style="font-family:sans-serif;max-width:560px;margin:0 auto;background:#0f172a;padding:32px;border-radius:12px;">
        <div style="text-align:center;margin-bottom:24px;">
            <div style="display:inline-flex;align-items:center;justify-content:center;
                        width:56px;height:56px;background:#14532d;border-radius:50%;margin-bottom:12px;">
                <span style="font-size:28px;">&#10003;</span>
            </div>
            <h2 style="color:#f1f5f9;margin:0;">Your file is ready!</h2>
        </div>

        <p style="color:#94a3b8;text-align:center;">
            Your Glacier Deep Archive restore has completed successfully.
        </p>

        <div style="background:#1e293b;border-radius:8px;padding:16px;margin:20px 0;
                    border-left:4px solid #22c55e;display:flex;align-items:center;gap:12px;">
            <span style="font-size:24px;">&#128196;</span>
            <p style="color:#e2e8f0;margin:0;font-weight:600;">{file_name}</p>
        </div>

        <div style="text-align:center;margin:28px 0;">
            <a href="{drive_url}"
               style="display:inline-block;background:#0ea5e9;color:#fff;text-decoration:none;
                      font-weight:600;padding:12px 28px;border-radius:8px;font-size:15px;">
                Go to NovaDrive &rarr;
            </a>
        </div>

        <div style="background:#172554;border:1px solid #1e3a8a;border-radius:8px;padding:12px 16px;margin:20px 0;">
            <p style="color:#93c5fd;font-size:13px;margin:0;">
                &#9432;&nbsp; The restored copy is available for <strong>7 days</strong>.
                After that it will return to Deep Archive automatically.
            </p>
        </div>

        <hr style="border:none;border-top:1px solid #1e293b;margin:24px 0;">
        <p style="color:#475569;font-size:12px;margin:0;text-align:center;">
            NovaDrive &nbsp;·&nbsp; nodepulsecaringal.xyz
        </p>
    </div>
    """
