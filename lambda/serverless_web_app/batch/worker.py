"""
NovaDrive Batch Worker
Runs inside a Fargate container submitted by AWS Batch.

Required env vars:
  JOB_TYPE                  zip_folder
  FOLDER_IDS                comma-separated folder UUIDs
  OWNER_SUB                 Cognito user sub
  JOB_DB_ID                 BatchJob UUID
  DRIVE_BUCKET_NAME         S3 bucket
  AWS_REGION                e.g. ap-southeast-2
  DYNAMODB_FOLDERS_TABLE    DynamoDB folders table name
  DYNAMODB_FILES_TABLE      DynamoDB files table name
  DYNAMODB_BATCH_JOBS_TABLE DynamoDB batch-jobs table name
"""

import io
import logging
import os
import sys
import uuid
import zipfile

import boto3

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)


def _setup_django():
    app_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if app_root not in sys.path:
        sys.path.insert(0, app_root)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")
    os.environ.setdefault("DJANGO_SECRET_KEY", "batch-worker-placeholder")
    os.environ.setdefault("ALLOWED_HOSTS", "*")
    import django
    django.setup()


def _collect_files(folder_id, owner_sub):
    from drive import dal
    from drive.models import DriveFile

    accessible = {DriveFile.GLACIER_IR}
    result = []
    queue = [(folder_id, "")]
    visited = set()

    while queue:
        fid, prefix = queue.pop(0)
        if fid in visited:
            continue
        visited.add(fid)

        for sf in dal.list_subfolders(owner_sub, fid):
            child_prefix = f"{prefix}/{sf.name}" if prefix else sf.name
            queue.append((sf.folder_id, child_prefix))

        for f in dal.list_files_in_folder(fid):
            if f.owner_sub != owner_sub or f.is_deleted() or f.storage_class not in accessible:
                continue
            arc_path = f"{prefix}/{f.name}" if prefix else f.name
            result.append((f, arc_path))

    return result


def run_zip_folder(folder_ids, owner_sub, job_db_id):
    from drive import dal
    from drive.models import BatchJob

    folders = [dal.get_folder(fid) for fid in folder_ids]
    folders = [f for f in folders if f and f.owner_sub == owner_sub]
    if not folders:
        raise RuntimeError(f"No folders found for ids={folder_ids} owner={owner_sub}")

    bucket = os.environ["DRIVE_BUCKET_NAME"]
    region = os.environ.get("AWS_REGION", "ap-southeast-2")
    s3 = boto3.client("s3", region_name=region)

    all_files = []
    for folder in folders:
        for drv_file, rel_path in _collect_files(folder.folder_id, owner_sub):
            all_files.append((folder.name, drv_file, rel_path))

    total = len(all_files)
    log.info("Total files to zip: %d across %d folder(s)", total, len(folders))

    last_reported_pct = 0
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, (folder_name, drv_file, rel_path) in enumerate(all_files):
            arc_path = f"{folder_name}/{rel_path}"
            try:
                obj = s3.get_object(Bucket=bucket, Key=drv_file.s3_key)
                zf.writestr(arc_path, obj["Body"].read())
                log.info("  [%d/%d] added %s", i + 1, total, arc_path)
            except Exception as e:
                log.warning("  skip %s: %s", drv_file.s3_key, e)

            if total > 0:
                pct = min(int((i + 1) / total * 90), 90)
                if pct >= last_reported_pct + 5:
                    last_reported_pct = pct
                    dal.set_batch_job_progress(job_db_id, pct)

    dal.set_batch_job_progress(job_db_id, 95)
    buf.seek(0)
    zip_key = f"temp-zips/{uuid.uuid4()}.zip"
    s3.put_object(Bucket=bucket, Key=zip_key, Body=buf.getvalue(),
                  ContentType="application/zip")
    log.info("Uploaded zip to s3://%s/%s", bucket, zip_key)

    dal.set_batch_job_ready(job_db_id, zip_key)
    log.info("Job %s marked READY", job_db_id)


def main():
    job_type       = os.environ.get("JOB_TYPE", "zip_folder")
    folder_ids_str = os.environ.get("FOLDER_IDS", "")
    owner_sub      = os.environ["OWNER_SUB"]
    job_db_id      = os.environ["JOB_DB_ID"]
    folder_ids     = [x.strip() for x in folder_ids_str.split(",") if x.strip()]

    _setup_django()

    from drive import dal
    from drive.models import BatchJob
    dal.set_batch_job_running(job_db_id)

    try:
        if job_type == "zip_folder":
            run_zip_folder(folder_ids, owner_sub, job_db_id)
        else:
            raise RuntimeError(f"Unknown JOB_TYPE: {job_type}")
    except Exception as e:
        log.error("Job %s failed: %s", job_db_id, e, exc_info=True)
        dal.set_batch_job_failed(job_db_id)
        sys.exit(1)


if __name__ == "__main__":
    main()
