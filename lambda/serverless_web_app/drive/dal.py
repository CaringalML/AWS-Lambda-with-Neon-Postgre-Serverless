import os
import uuid
from datetime import datetime, timezone, timedelta

import boto3
from boto3.dynamodb.conditions import Key, Attr
from django.conf import settings

from .models import DriveFile, DriveFolder, BatchJob, _ListProxy

ROOT = "ROOT"  # sentinel stored in DynamoDB for null parent_id / folder_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ddb():
    return boto3.resource("dynamodb", region_name=settings.AWS_REGION)

def _folders_table():
    return _ddb().Table(os.environ["DYNAMODB_FOLDERS_TABLE"])

def _files_table():
    return _ddb().Table(os.environ["DYNAMODB_FILES_TABLE"])

def _batch_jobs_table():
    return _ddb().Table(os.environ["DYNAMODB_BATCH_JOBS_TABLE"])

def _now():
    return datetime.now(timezone.utc).isoformat()

def _query_all(table, **kwargs):
    """Query with automatic pagination — handles DynamoDB 1 MB page limit."""
    items = []
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return items

def _iso(dt):
    if dt is None:
        return None
    return dt if isinstance(dt, str) else dt.isoformat()


# ---------------------------------------------------------------------------
# Deserializers
# ---------------------------------------------------------------------------

def _folder_from(item):
    if not item:
        return None
    return DriveFolder(
        folder_id=item["folder_id"],
        owner_sub=item["owner_sub"],
        name=item["name"],
        parent_id=None if item.get("parent_id") == ROOT else item.get("parent_id"),
        created_at=item.get("created_at", ""),
        deleted_at=item.get("deleted_at"),
    )

def _file_from(item):
    if not item:
        return None
    return DriveFile(
        file_id=item["file_id"],
        owner_sub=item["owner_sub"],
        folder_id=None if item.get("folder_id") == ROOT else item.get("folder_id"),
        name=item["name"],
        s3_key=item["s3_key"],
        size=int(item.get("size", 0)),
        content_type=item.get("content_type", "application/octet-stream"),
        storage_class=item.get("storage_class", DriveFile.GLACIER_IR),
        uploaded_at=item.get("uploaded_at", ""),
        restore_status=item.get("restore_status", ""),
        restore_notify_email=item.get("restore_notify_email", ""),
        restore_expires_at=item.get("restore_expires_at"),
        deleted_at=item.get("deleted_at"),
        captured_at=item.get("captured_at"),
    )

def _job_from(item):
    if not item:
        return None
    return BatchJob(
        job_id=item["job_id"],
        owner_sub=item["owner_sub"],
        aws_job_id=item.get("aws_job_id", ""),
        type=item.get("type", "zip_folder"),
        folder_name=item.get("folder_name", ""),
        status=item.get("status", BatchJob.PENDING),
        result_key=item.get("result_key", ""),
        progress=int(item.get("progress", 0)),
        created_at=item.get("created_at", ""),
        expires_at=item.get("expires_at"),
    )


# ---------------------------------------------------------------------------
# Folder operations
# ---------------------------------------------------------------------------

def get_folder(folder_id):
    resp = _folders_table().get_item(Key={"folder_id": folder_id})
    return _folder_from(resp.get("Item"))

def list_subfolders(owner_sub, parent_id, active_only=True):
    """List immediate children of parent_id (None = root level), sorted by name."""
    parent_key = parent_id if parent_id else ROOT
    fe = Attr("owner_sub").eq(owner_sub)
    if active_only:
        fe = fe & Attr("deleted_at").not_exists()
    items = _query_all(
        _folders_table(),
        IndexName="parent-index",
        KeyConditionExpression=Key("parent_id").eq(parent_key),
        FilterExpression=fe,
    )
    return sorted([_folder_from(i) for i in items], key=lambda f: f.name.lower())

def list_all_folders(owner_sub):
    """All folders for owner — active and deleted — sorted by created_at desc."""
    items = _query_all(
        _folders_table(),
        IndexName="owner-created-index",
        KeyConditionExpression=Key("owner_sub").eq(owner_sub),
        ScanIndexForward=False,
    )
    return [_folder_from(i) for i in items]

def create_folder(owner_sub, name, parent_id=None):
    folder_id = str(uuid.uuid4())
    item = {
        "folder_id":  folder_id,
        "owner_sub":  owner_sub,
        "name":       name,
        "parent_id":  parent_id if parent_id else ROOT,
        "created_at": _now(),
    }
    _folders_table().put_item(Item=item)
    return _folder_from(item)

def soft_delete_folder(folder_id):
    now = _now()
    _folders_table().update_item(
        Key={"folder_id": folder_id},
        UpdateExpression="SET deleted_at = :d",
        ExpressionAttributeValues={":d": now},
    )
    return now

def restore_folder(folder_id):
    _folders_table().update_item(
        Key={"folder_id": folder_id},
        UpdateExpression="REMOVE deleted_at",
    )

def rename_folder(folder_id, name):
    _folders_table().update_item(
        Key={"folder_id": folder_id},
        UpdateExpression="SET #n = :n",
        ExpressionAttributeNames={"#n": "name"},
        ExpressionAttributeValues={":n": name},
    )

def hard_delete_folder(folder_id):
    _folders_table().delete_item(Key={"folder_id": folder_id})


# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------

def get_file(file_id):
    resp = _files_table().get_item(Key={"file_id": file_id})
    return _file_from(resp.get("Item"))

def get_file_by_s3key(s3_key):
    resp = _files_table().query(
        IndexName="s3key-index",
        KeyConditionExpression=Key("s3_key").eq(s3_key),
        Limit=1,
    )
    items = resp.get("Items", [])
    return _file_from(items[0]) if items else None

def list_files_in_folder(folder_id, active_only=True):
    """Files in a specific folder (None = root), newest first."""
    folder_key = folder_id if folder_id else ROOT
    kwargs = dict(
        IndexName="folder-index",
        KeyConditionExpression=Key("folder_id").eq(folder_key),
        ScanIndexForward=False,
    )
    if active_only:
        kwargs["FilterExpression"] = Attr("deleted_at").not_exists()
    return [_file_from(i) for i in _query_all(_files_table(), **kwargs)]

def list_all_files(owner_sub):
    """All files for owner — active and deleted — newest first."""
    items = _query_all(
        _files_table(),
        IndexName="owner-index",
        KeyConditionExpression=Key("owner_sub").eq(owner_sub),
        ScanIndexForward=False,
    )
    return [_file_from(i) for i in items]

def create_file(owner_sub, name, s3_key, size, content_type,
                folder_id=None, storage_class=None, captured_at=None):
    file_id = str(uuid.uuid4())
    item = {
        "file_id":              file_id,
        "owner_sub":            owner_sub,
        "folder_id":            folder_id if folder_id else ROOT,
        "name":                 name,
        "s3_key":               s3_key,
        "size":                 size,
        "content_type":         content_type,
        "storage_class":        storage_class or DriveFile.GLACIER_IR,
        "uploaded_at":          _now(),
        "restore_status":       "",
        "restore_notify_email": "",
    }
    if captured_at is not None:
        item["captured_at"] = _iso(captured_at)
    _files_table().put_item(Item=item)
    return _file_from(item)

def upsert_file(s3_key, owner_sub, name, size, content_type,
                folder_id=None, storage_class=None, captured_at=None):
    """Create or overwrite a file record by s3_key. Returns (DriveFile, created_bool)."""
    existing = get_file_by_s3key(s3_key)
    if existing:
        expr_vals = {
            ":os":  owner_sub,
            ":n":   name,
            ":sz":  size,
            ":ct":  content_type,
            ":fid": folder_id if folder_id else ROOT,
            ":sc":  storage_class or DriveFile.GLACIER_IR,
            ":rs":  "",
            ":rn":  "",
        }
        set_parts = (
            "owner_sub=:os, #n=:n, size=:sz, content_type=:ct, "
            "folder_id=:fid, storage_class=:sc, restore_status=:rs, restore_notify_email=:rn"
        )
        if captured_at is not None:
            set_parts += ", captured_at=:ca"
            expr_vals[":ca"] = _iso(captured_at)
        _files_table().update_item(
            Key={"file_id": existing.file_id},
            UpdateExpression=f"SET {set_parts} REMOVE deleted_at, restore_expires_at",
            ExpressionAttributeNames={"#n": "name"},
            ExpressionAttributeValues=expr_vals,
        )
        return get_file(existing.file_id), False
    return create_file(owner_sub, name, s3_key, size, content_type,
                       folder_id, storage_class, captured_at), True

def soft_delete_file(file_id):
    _files_table().update_item(
        Key={"file_id": file_id},
        UpdateExpression="SET deleted_at = :d",
        ExpressionAttributeValues={":d": _now()},
    )

def restore_file(file_id):
    _files_table().update_item(
        Key={"file_id": file_id},
        UpdateExpression="REMOVE deleted_at",
    )

def hard_delete_file(file_id):
    _files_table().delete_item(Key={"file_id": file_id})

def update_file_s3key(file_id, s3_key):
    _files_table().update_item(
        Key={"file_id": file_id},
        UpdateExpression="SET s3_key = :k",
        ExpressionAttributeValues={":k": s3_key},
    )

def update_file_name_and_key(file_id, name, s3_key):
    _files_table().update_item(
        Key={"file_id": file_id},
        UpdateExpression="SET #n = :n, s3_key = :k",
        ExpressionAttributeNames={"#n": "name"},
        ExpressionAttributeValues={":n": name, ":k": s3_key},
    )

def update_file_storage_class(file_id, storage_class):
    _files_table().update_item(
        Key={"file_id": file_id},
        UpdateExpression="SET storage_class = :sc",
        ExpressionAttributeValues={":sc": storage_class},
    )

def update_file_restore(file_id, status, notify_email="", expires_at=None):
    expr_vals = {":rs": status, ":rn": notify_email}
    if expires_at:
        _files_table().update_item(
            Key={"file_id": file_id},
            UpdateExpression="SET restore_status=:rs, restore_notify_email=:rn, restore_expires_at=:re",
            ExpressionAttributeValues={**expr_vals, ":re": _iso(expires_at)},
        )
    else:
        _files_table().update_item(
            Key={"file_id": file_id},
            UpdateExpression="SET restore_status=:rs, restore_notify_email=:rn",
            ExpressionAttributeValues=expr_vals,
        )

def clear_restore_status(file_id):
    _files_table().update_item(
        Key={"file_id": file_id},
        UpdateExpression="SET restore_status=:rs REMOVE restore_expires_at",
        ExpressionAttributeValues={":rs": ""},
    )


# ---------------------------------------------------------------------------
# BatchJob operations
# ---------------------------------------------------------------------------

def create_batch_job(owner_sub, job_type, folder_name):
    job_id = str(uuid.uuid4())
    item = {
        "job_id":      job_id,
        "owner_sub":   owner_sub,
        "aws_job_id":  "",
        "type":        job_type,
        "folder_name": folder_name,
        "status":      BatchJob.PENDING,
        "result_key":  "",
        "progress":    0,
        "created_at":  _now(),
    }
    _batch_jobs_table().put_item(Item=item)
    return _job_from(item)

def get_batch_job(job_id):
    resp = _batch_jobs_table().get_item(Key={"job_id": job_id})
    return _job_from(resp.get("Item"))

def set_batch_job_aws_id(job_id, aws_job_id):
    _batch_jobs_table().update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET aws_job_id = :j",
        ExpressionAttributeValues={":j": aws_job_id},
    )

def set_batch_job_failed(job_id):
    _batch_jobs_table().update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET #s = :s",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": BatchJob.FAILED},
    )

def set_batch_job_running(job_id):
    _batch_jobs_table().update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET #s = :s",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": BatchJob.RUNNING},
    )

def set_batch_job_progress(job_id, pct):
    _batch_jobs_table().update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET progress = :p",
        ExpressionAttributeValues={":p": pct},
    )

def set_batch_job_ready(job_id, result_key):
    expires = datetime.now(timezone.utc) + timedelta(hours=24)
    _batch_jobs_table().update_item(
        Key={"job_id": job_id},
        UpdateExpression=(
            "SET #s = :s, result_key = :rk, progress = :p, expires_at = :ea, #ttl = :ttl"
        ),
        ExpressionAttributeNames={"#s": "status", "#ttl": "ttl"},
        ExpressionAttributeValues={
            ":s":   BatchJob.READY,
            ":rk":  result_key,
            ":p":   100,
            ":ea":  expires.isoformat(),
            ":ttl": int(expires.timestamp()),
        },
    )
