import datetime
import itertools
import json
import logging
import base64
import math
import os
from urllib.parse import quote, quote_plus

import resend

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.signers import CloudFrontSigner
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from django.conf import settings
from django.http import JsonResponse, HttpResponse, Http404
from django.shortcuts import render, redirect
from django.views.decorators.http import require_POST

from accounts.decorators import cognito_login_required
from .models import DriveFile, BatchJob, UploadFailure, _ListProxy
from . import dal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level cache — survives across Lambda invocations in the same container
# ---------------------------------------------------------------------------
_cf_private_key_cache = None

_STORAGE_CAP_BYTES = 15 * 1024 ** 3  # 15 GB display cap


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_dt(s):
    if not s:
        return None
    dt = s if isinstance(s, datetime.datetime) else datetime.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _get_folder_path(folder_id, owner_sub):
    """Walk the folder parent chain and return a safe S3 path string."""
    if not folder_id:
        return None
    parts = []
    visited = set()
    fid = folder_id
    while fid and fid not in visited:
        visited.add(fid)
        folder = dal.get_folder(fid)
        if not folder or folder.owner_sub != owner_sub:
            break
        safe = "".join(
            c if c.isalnum() or c in "-_. " else "_" for c in folder.name
        ).strip() or "_"
        parts.insert(0, safe)
        fid = folder.parent_id
    return "/".join(parts) if parts else None


def _get_resend_api_key():
    if key := os.environ.get("RESEND_API_KEY"):
        return key
    param_name = settings.SSM_RESEND_API_KEY_NAME
    if param_name:
        ssm = boto3.client("ssm", region_name=settings.AWS_REGION)
        return ssm.get_parameter(Name=param_name, WithDecryption=True)["Parameter"]["Value"]
    return ""


def _s3():
    return boto3.client(
        "s3",
        region_name=settings.AWS_REGION,
        endpoint_url=f"https://s3.{settings.AWS_REGION}.amazonaws.com",
        config=Config(signature_version="s3v4"),
    )


def _get_owner_sub(request):
    return request.session.get("user_sub", "")


# Signed URLs expire on fixed 6-hour boundaries rather than N seconds from
# "now" — otherwise every page load mints a different URL for the same object
# and the browser cache can never be reused.
_URL_EXPIRY_WINDOW = 6 * 3600


def _get_cloudfront_signed_url(s3_key, expires_seconds=300):
    global _cf_private_key_cache
    if _cf_private_key_cache is None:
        ssm = boto3.client("ssm", region_name=settings.AWS_REGION)
        pem = ssm.get_parameter(
            Name=settings.CLOUDFRONT_PRIVATE_KEY_SSM_NAME,
            WithDecryption=True,
        )["Parameter"]["Value"]
        _cf_private_key_cache = serialization.load_pem_private_key(pem.encode(), password=None)

    def rsa_signer(message):
        return _cf_private_key_cache.sign(message, padding.PKCS1v15(), hashes.SHA1())

    cf_signer = CloudFrontSigner(settings.CLOUDFRONT_KEY_PAIR_ID, rsa_signer)
    encoded_key = quote(s3_key, safe="/")
    url = f"https://{settings.CLOUDFRONT_DOMAIN}/{encoded_key}"
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    expire_epoch = (int(now + expires_seconds) // _URL_EXPIRY_WINDOW + 1) * _URL_EXPIRY_WINDOW
    expire_at = datetime.datetime.fromtimestamp(expire_epoch, tz=datetime.timezone.utc)
    return cf_signer.generate_presigned_url(url, date_less_than=expire_at)


def _build_breadcrumbs(folder):
    """Walk up the parent chain and return [root, ..., folder]."""
    crumbs = []
    visited = set()
    node = folder
    while node and node.folder_id not in visited:
        visited.add(node.folder_id)
        crumbs.insert(0, node)
        node = dal.get_folder(node.parent_id) if node.parent_id else None
    return crumbs


def _build_sidebar_tree(owner_sub):
    """Build 3-level folder tree for the sidebar."""
    root_folders = dal.list_subfolders(owner_sub, None, active_only=True)
    for f in root_folders:
        level2 = dal.list_subfolders(owner_sub, f.folder_id, active_only=True)
        for sf in level2:
            level3 = dal.list_subfolders(owner_sub, sf.folder_id, active_only=True)
            sf.subfolders = _ListProxy(level3)
        f.subfolders = _ListProxy(level2)
    return root_folders


def _storage_stats(owner_sub):
    """Returns (bytes, display, pct, active_file_count). The count rides along
    so callers don't scan the library a second time just to count it."""
    active = [f for f in dal.list_all_files(owner_sub) if not f.deleted_at]
    raw = sum(f.size for f in active)
    pct = min(100, round(raw / _STORAGE_CAP_BYTES * 100, 1))
    total = float(raw)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if total < 1024:
            return raw, f"{total:.1f} {unit}", pct, len(active)
        total /= 1024
    return raw, f"{total:.1f} PB", pct, len(active)


def _collect_folder_ids(folder, owner_sub):
    """BFS walk of folder subtree; returns all folder IDs including root."""
    ids = []
    queue = [folder.folder_id]
    visited = set()
    while queue:
        fid = queue.pop()
        if fid in visited:
            continue
        visited.add(fid)
        ids.append(fid)
        for sf in dal.list_subfolders(owner_sub, fid, active_only=False):
            queue.append(sf.folder_id)
    return ids


def _s3_move(s3, old_key, new_key):
    s3.copy_object(
        Bucket=settings.DRIVE_BUCKET_NAME,
        CopySource={"Bucket": settings.DRIVE_BUCKET_NAME, "Key": old_key},
        Key=new_key,
        MetadataDirective="COPY",
    )
    s3.delete_object(Bucket=settings.DRIVE_BUCKET_NAME, Key=old_key)


def _thumb_key(s3_key):
    return f"thumbs/{s3_key}.webp"


def _preview_key(s3_key):
    return f"previews/{s3_key}.webp"


def _move_thumb(s3, old_key, new_key):
    for derive in (_thumb_key, _preview_key):
        try:
            _s3_move(s3, derive(old_key), derive(new_key))
        except ClientError:
            pass  # derivative missing (non-image, or generation in flight)


def _delete_object_and_thumb(s3, s3_key):
    for key in (s3_key, _thumb_key(s3_key), _preview_key(s3_key)):
        try:
            s3.delete_object(Bucket=settings.DRIVE_BUCKET_NAME, Key=key)
        except ClientError:
            pass


def _request_thumbnail(s3_key):
    """Fire-and-forget invoke of the thumbnailer Lambda with a synthetic S3
    event — used to backfill files uploaded before thumbnails existed."""
    fn = settings.THUMBNAILER_FUNCTION
    if not fn:
        return
    try:
        boto3.client("lambda", region_name=settings.AWS_REGION).invoke(
            FunctionName=fn,
            InvocationType="Event",
            Payload=json.dumps({"Records": [{"s3": {
                "bucket": {"name": settings.DRIVE_BUCKET_NAME},
                # thumbnailer unquote_plus()es keys like real S3 events
                "object": {"key": quote_plus(s3_key)},
            }}]}).encode(),
        )
    except Exception as e:
        logger.warning("thumbnail backfill request failed for %s: %s", s3_key, e)


# Rows per listing page. A full folder overruns Lambda's 6 MB response cap at
# roughly 1,300 files, so listings are paged rather than rendered whole.
_PAGE_SIZE = 150


def _encode_cursor(key):
    """DynamoDB LastEvaluatedKey -> opaque string safe for a query param."""
    if not key:
        return ""
    return base64.urlsafe_b64encode(json.dumps(key).encode()).decode()


def _decode_cursor(raw):
    if not raw:
        return None
    try:
        key = json.loads(base64.urlsafe_b64decode(raw.encode()).decode())
        return key if isinstance(key, dict) else None
    except Exception:
        return None  # tampered or stale cursor — start from the top


def _view_mode(request):
    """List or grid. A cookie rather than localStorage so the server can render
    just the one layout instead of shipping both in every row."""
    return "grid" if request.COOKIES.get("nd_view") == "grid" else "list"


def _drive_visible(f, now):
    """Does this file belong in a drive listing? Expires a lapsed restore
    window on the way past, which is what the old full-library sweep did."""
    if (f.restore_status == DriveFile.RESTORE_READY
            and f.restore_expires_at
            and _parse_dt(f.restore_expires_at) < now):
        dal.clear_restore_status(f.file_id)
        f.restore_status = ""
        f.restore_expires_at = None
    if f.deleted_at:
        return False
    return (f.storage_class == DriveFile.GLACIER_IR
            or (f.storage_class == DriveFile.DEEP_ARCHIVE
                and f.restore_status == DriveFile.RESTORE_READY))


def _require_folder(folder_id, owner_sub):
    folder = dal.get_folder(folder_id)
    if not folder or folder.owner_sub != owner_sub:
        raise Http404
    return folder


def _parse_captured_at(cap_str):
    """EXIF/filename capture date from the browser — bad values are not fatal."""
    if not cap_str:
        return None
    try:
        dt = datetime.datetime.fromisoformat(cap_str.replace("Z", "+00:00"))
        return dt.replace(tzinfo=datetime.timezone.utc) if dt.tzinfo is None else dt
    except (ValueError, AttributeError):
        return None


def _require_file(file_id, owner_sub):
    f = dal.get_file(file_id)
    if not f or f.owner_sub != owner_sub:
        raise Http404
    return f


def _failed_upload_count(owner_sub):
    """Badge count for the sidebar — one extra Query per full page render."""
    try:
        return len(dal.list_upload_failures(owner_sub))
    except Exception as e:
        # A missing table (pre-Terraform-apply) must not 500 the whole drive.
        logger.warning("failed-upload count unavailable: %s", e)
        return 0


# ---------------------------------------------------------------------------
# Views — Drive home
# ---------------------------------------------------------------------------

@cognito_login_required
def drive_home(request, folder_pk=None):
    owner_sub = _get_owner_sub(request)
    current_folder = None
    breadcrumbs = []
    if folder_pk:
        current_folder = _require_folder(folder_pk, owner_sub)
        breadcrumbs = _build_breadcrumbs(current_folder)

    now = datetime.datetime.now(datetime.timezone.utc)
    q = request.GET.get("q", "").strip()
    current_folder_id = current_folder.folder_id if current_folder else None
    after = _decode_cursor(request.GET.get("after"))

    def visible(f):
        return _drive_visible(f, now)

    truncated = 0
    next_cursor = ""

    if q:
        # No index on name, so search still reads the library — but it returns
        # a page of it, or a big result set would blow the response cap too.
        ql = q.lower()
        matches = [f for f in dal.list_all_files(owner_sub)
                   if ql in f.name.lower() and visible(f)]
        files = matches[:_PAGE_SIZE]
        truncated = len(matches) - len(files)
        subfolders = [fld for fld in dal.list_all_folders(owner_sub)
                      if not fld.deleted_at and ql in fld.name.lower()]
    else:
        # One folder-index page instead of reading every file the owner has
        files, next_key = dal.list_files_page(
            current_folder_id, after=after, limit=_PAGE_SIZE, keep=visible,
        )
        next_cursor = _encode_cursor(next_key)
        # Folders belong to the first page only; "load more" is files alone
        subfolders = ([] if after else
                      dal.list_subfolders(owner_sub, current_folder_id, active_only=True))

    ctx = {
        "files":          files,
        "subfolders":     subfolders,
        "current_folder": current_folder,
        "breadcrumbs":    breadcrumbs,
        "search_query":   q,
        "next_cursor":    next_cursor,
        "truncated":      truncated,
        "view_mode":      _view_mode(request),
    }

    # Infinite scroll asking for the next batch — rows and nothing else
    if after:
        return render(request, "drive/partials/file_page.html", ctx)

    if request.headers.get("HX-Request"):
        return render(request, "drive/partials/search_results.html", ctx)

    ctx["sidebar_folders"] = _build_sidebar_tree(owner_sub)
    _, ctx["storage_used"], ctx["storage_pct"], ctx["total_files"] = _storage_stats(owner_sub)
    ctx["failed_count"] = _failed_upload_count(owner_sub)

    response = render(request, "drive/home.html", ctx)
    response["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# Folder CRUD
# ---------------------------------------------------------------------------

@cognito_login_required
@require_POST
def create_folder(request):
    try:
        data = json.loads(request.body)
        name = data.get("name", "").strip()
        parent_pk = data.get("parent_pk")
        owner_sub = _get_owner_sub(request)

        if not name:
            return JsonResponse({"error": "Folder name is required"}, status=400)

        parent_id = None
        if parent_pk:
            parent = _require_folder(parent_pk, owner_sub)
            parent_id = parent.folder_id

        folder = dal.create_folder(owner_sub, name, parent_id)
        html = render(request, "drive/partials/folder_row.html", {"folder": folder}).content.decode()
        return JsonResponse({"html": html, "id": folder.folder_id})
    except Http404:
        return JsonResponse({"error": "Parent folder not found"}, status=404)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)


@cognito_login_required
@require_POST
def delete_folder(request, pk):
    owner_sub = _get_owner_sub(request)
    folder = _require_folder(pk, owner_sub)
    if folder.deleted_at:
        raise Http404
    dal.soft_delete_folder(folder.folder_id)
    return HttpResponse("")


@cognito_login_required
@require_POST
def rename_folder(request, pk):
    owner_sub = _get_owner_sub(request)
    folder = _require_folder(pk, owner_sub)
    if folder.deleted_at:
        raise Http404
    try:
        data = json.loads(request.body)
        name = data.get("name", "").strip()
        if not name:
            return JsonResponse({"error": "Name cannot be empty."}, status=400)

        old_path = _get_folder_path(pk, owner_sub)
        old_prefix = f"{owner_sub}/{old_path}/" if old_path else f"{owner_sub}/"

        dal.rename_folder(folder.folder_id, name)

        new_path = _get_folder_path(pk, owner_sub)
        new_prefix = f"{owner_sub}/{new_path}/" if new_path else f"{owner_sub}/"

        if old_prefix != new_prefix:
            s3 = _s3()
            for fid in _collect_folder_ids(folder, owner_sub):
                for f in dal.list_files_in_folder(fid, active_only=False):
                    if f.s3_key.startswith(old_prefix):
                        new_key = new_prefix + f.s3_key[len(old_prefix):]
                        try:
                            _s3_move(s3, f.s3_key, new_key)
                            _move_thumb(s3, f.s3_key, new_key)
                            dal.update_file_s3key(f.file_id, new_key)
                        except ClientError as e:
                            logger.error("S3 move failed %s → %s: %s", f.s3_key, new_key, e)

        return JsonResponse({"id": folder.folder_id, "name": name})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)


# ---------------------------------------------------------------------------
# File CRUD
# ---------------------------------------------------------------------------

@cognito_login_required
@require_POST
def rename_file(request, pk):
    owner_sub = _get_owner_sub(request)
    file = _require_file(pk, owner_sub)
    if file.deleted_at:
        raise Http404
    try:
        data = json.loads(request.body)
        name = data.get("name", "").strip()
        if not name:
            return JsonResponse({"error": "Name cannot be empty."}, status=400)

        old_key = file.s3_key
        directory = old_key.rsplit("/", 1)[0] if "/" in old_key else owner_sub
        new_key = f"{directory}/{name}"

        if old_key != new_key:
            s3 = _s3()
            try:
                _s3_move(s3, old_key, new_key)
                _move_thumb(s3, old_key, new_key)
            except ClientError as e:
                logger.error("S3 move failed %s → %s: %s", old_key, new_key, e)
                new_key = old_key  # keep old key if move failed

        dal.update_file_name_and_key(file.file_id, name, new_key)
        return JsonResponse({"id": file.file_id, "name": name})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)


@cognito_login_required
@require_POST
def upload_url(request):
    try:
        if not settings.DRIVE_BUCKET_NAME:
            return JsonResponse(
                {"error": "DRIVE_BUCKET_NAME not set — has Terraform been applied?"},
                status=500,
            )

        data = json.loads(request.body)
        filename     = data.get("filename", "unnamed")
        content_type = data.get("content_type", "application/octet-stream")
        owner_sub    = _get_owner_sub(request)
        folder_pk    = data.get("folder_pk")

        folder_path = _get_folder_path(folder_pk, owner_sub)
        s3_key = f"{owner_sub}/{folder_path}/{filename}" if folder_path else f"{owner_sub}/{filename}"

        existing = dal.get_file_by_s3key(s3_key)
        exists = existing is not None and not existing.deleted_at

        presigned = _s3().generate_presigned_post(
            Bucket=settings.DRIVE_BUCKET_NAME,
            Key=s3_key,
            Fields={"Content-Type": content_type},
            Conditions=[
                {"Content-Type": content_type},
                # This path only carries sub-threshold files now; anything
                # larger goes through multipart.
                ["content-length-range", 1, settings.MULTIPART_THRESHOLD],
            ],
            # The policy is checked when the upload lands, so this has to
            # outlast the transfer, not just the click that started it.
            ExpiresIn=3600,
        )
        return JsonResponse({"url": presigned["url"], "fields": presigned["fields"],
                             "s3_key": s3_key, "exists": exists,
                             "max_bytes": settings.MULTIPART_THRESHOLD})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)


@cognito_login_required
@require_POST
def confirm_upload(request):
    try:
        data = json.loads(request.body)
        owner_sub = _get_owner_sub(request)
        folder_pk = data.get("folder_pk")

        folder_id = None
        if folder_pk:
            folder = _require_folder(folder_pk, owner_sub)
            folder_id = folder.folder_id

        head = _s3().head_object(Bucket=settings.DRIVE_BUCKET_NAME, Key=data["s3_key"])
        captured_at = _parse_captured_at(data.get("captured_at"))

        drive_file, created = dal.upsert_file(
            s3_key=data["s3_key"],
            owner_sub=owner_sub,
            name=data["filename"],
            size=head["ContentLength"],
            content_type=head.get("ContentType", "application/octet-stream"),
            folder_id=folder_id,
            storage_class=DriveFile.GLACIER_IR,
            captured_at=captured_at,
        )

        _s3().copy_object(
            Bucket=settings.DRIVE_BUCKET_NAME,
            CopySource={"Bucket": settings.DRIVE_BUCKET_NAME, "Key": data["s3_key"]},
            Key=data["s3_key"],
            StorageClass=DriveFile.GLACIER_IR,
            MetadataDirective="COPY",
        )

        html = render(request, "drive/partials/file_row.html", {"file": drive_file}).content.decode()
        _, storage_used, _, _ = _storage_stats(owner_sub)
        return JsonResponse({"html": html, "id": drive_file.file_id,
                             "storage_used": storage_used, "overwritten": not created})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)


# ---------------------------------------------------------------------------
# Multipart upload — the path for anything above MULTIPART_THRESHOLD
# ---------------------------------------------------------------------------

def _owned_key(s3_key, owner_sub):
    """The browser supplies the key on every multipart call, so verify it sits
    inside the caller's own prefix before signing anything against it."""
    if not owner_sub or not s3_key or not s3_key.startswith(f"{owner_sub}/"):
        raise Http404
    return s3_key


def _part_size_for(size):
    """Part size that keeps the object under S3's hard 10,000-part ceiling."""
    part = settings.MULTIPART_PART_SIZE
    if size > part * 9000:  # headroom under 10k
        mib = 1024 ** 2
        part = math.ceil(size / 9000 / mib) * mib
    return part


@cognito_login_required
@require_POST
def multipart_create(request):
    try:
        data = json.loads(request.body)
        owner_sub = _get_owner_sub(request)
        size = int(data.get("size", 0) or 0)

        if size > settings.MAX_UPLOAD_BYTES:
            return JsonResponse({"error": "File exceeds the upload limit."}, status=400)

        filename     = data.get("filename", "unnamed")
        content_type = data.get("content_type", "application/octet-stream")
        folder_path  = _get_folder_path(data.get("folder_pk"), owner_sub)
        s3_key = f"{owner_sub}/{folder_path}/{filename}" if folder_path else f"{owner_sub}/{filename}"

        existing = dal.get_file_by_s3key(s3_key)

        resp = _s3().create_multipart_upload(
            Bucket=settings.DRIVE_BUCKET_NAME,
            Key=s3_key,
            ContentType=content_type,
            # Set the storage class here rather than copying afterwards:
            # CopyObject caps at 5 GB and would fail on exactly the files
            # this path exists to carry.
            StorageClass=DriveFile.GLACIER_IR,
        )
        part_size = _part_size_for(size)
        return JsonResponse({
            "upload_id":  resp["UploadId"],
            "s3_key":     s3_key,
            "part_size":  part_size,
            "part_count": max(1, math.ceil(size / part_size)) if size else 1,
            "exists":     existing is not None and not existing.deleted_at,
        })
    except Exception as e:
        logger.error("multipart create failed: %s", e)
        return JsonResponse({"error": str(e)}, status=400)


@cognito_login_required
@require_POST
def multipart_urls(request):
    """Presign a batch of part URLs. Batched rather than all-at-once so a long
    upload isn't holding a thousand URLs that age out together."""
    try:
        data = json.loads(request.body)
        owner_sub = _get_owner_sub(request)
        s3_key    = _owned_key(data.get("s3_key"), owner_sub)
        upload_id = data.get("upload_id", "")
        numbers   = [int(n) for n in data.get("part_numbers", [])][:200]

        s3 = _s3()
        urls = {
            str(n): s3.generate_presigned_url(
                "upload_part",
                Params={
                    "Bucket":     settings.DRIVE_BUCKET_NAME,
                    "Key":        s3_key,
                    "UploadId":   upload_id,
                    "PartNumber": n,
                },
                ExpiresIn=settings.MULTIPART_URL_EXPIRY,
            )
            for n in numbers if 1 <= n <= 10000
        }
        return JsonResponse({"urls": urls})
    except Http404:
        raise
    except Exception as e:
        logger.error("multipart urls failed: %s", e)
        return JsonResponse({"error": str(e)}, status=400)


@cognito_login_required
@require_POST
def multipart_complete(request):
    try:
        data = json.loads(request.body)
        owner_sub = _get_owner_sub(request)
        s3_key    = _owned_key(data.get("s3_key"), owner_sub)

        parts = sorted(
            [{"PartNumber": int(p["PartNumber"]), "ETag": p["ETag"]}
             for p in data.get("parts", [])],
            key=lambda p: p["PartNumber"],
        )
        if not parts:
            return JsonResponse({"error": "No uploaded parts to assemble."}, status=400)

        folder_id = None
        if folder_pk := data.get("folder_pk"):
            folder = _require_folder(folder_pk, owner_sub)
            folder_id = folder.folder_id

        s3 = _s3()
        s3.complete_multipart_upload(
            Bucket=settings.DRIVE_BUCKET_NAME,
            Key=s3_key,
            UploadId=data.get("upload_id", ""),
            MultipartUpload={"Parts": parts},
        )

        head = s3.head_object(Bucket=settings.DRIVE_BUCKET_NAME, Key=s3_key)
        drive_file, created = dal.upsert_file(
            s3_key=s3_key,
            owner_sub=owner_sub,
            name=data["filename"],
            size=head["ContentLength"],
            content_type=head.get("ContentType", "application/octet-stream"),
            folder_id=folder_id,
            storage_class=DriveFile.GLACIER_IR,
            captured_at=_parse_captured_at(data.get("captured_at")),
        )
        # No copy_object — the storage class was set on create_multipart_upload.

        html = render(request, "drive/partials/file_row.html", {"file": drive_file}).content.decode()
        _, storage_used, _, _ = _storage_stats(owner_sub)
        return JsonResponse({"html": html, "id": drive_file.file_id,
                             "storage_used": storage_used, "overwritten": not created})
    except Http404:
        raise
    except Exception as e:
        logger.error("multipart complete failed: %s", e)
        return JsonResponse({"error": str(e)}, status=400)


@cognito_login_required
@require_POST
def multipart_abort(request):
    """Discard a half-finished upload so its parts stop costing storage.
    Best-effort — the bucket's abort-incomplete-multipart lifecycle rule
    sweeps anything that slips through after 7 days."""
    owner_sub = _get_owner_sub(request)
    try:
        data = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        data = {}

    # Validated outside the try below, so a key that isn't the caller's 404s
    # instead of being reported as a successful abort.
    s3_key = _owned_key(data.get("s3_key"), owner_sub)

    try:
        _s3().abort_multipart_upload(
            Bucket=settings.DRIVE_BUCKET_NAME,
            Key=s3_key,
            UploadId=data.get("upload_id", ""),
        )
    except Exception as e:
        logger.warning("multipart abort failed for %s: %s", s3_key, e)
    return JsonResponse({"aborted": True})


# ---------------------------------------------------------------------------
# File serving
# ---------------------------------------------------------------------------

@cognito_login_required
def download_file(request, pk):
    file = _require_file(pk, _get_owner_sub(request))
    if file.is_archived() and file.restore_status != DriveFile.RESTORE_READY:
        return HttpResponse("This file is archived and cannot be downloaded directly.", status=400)
    presigned_url = _s3().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": settings.DRIVE_BUCKET_NAME,
            "Key": file.s3_key,
            "ResponseContentDisposition": f'attachment; filename="{quote(file.name)}"',
            "ResponseContentType": file.content_type,
        },
        ExpiresIn=300,
    )
    return redirect(presigned_url)


@cognito_login_required
def get_file_url(request, pk):
    file = _require_file(pk, _get_owner_sub(request))
    if file.is_archived() and file.restore_status != DriveFile.RESTORE_READY:
        return JsonResponse(
            {"error": "archived", "message": "This file is archived and cannot be previewed."},
            status=400,
        )
    target_key = file.s3_key
    if file.content_type.startswith("image/"):
        # Lightbox gets the ~1600px WebP derivative (a few hundred KB)
        # instead of the multi-MB original; download still uses the original.
        preview = _preview_key(file.s3_key)
        try:
            head = _s3().head_object(Bucket=settings.DRIVE_BUCKET_NAME, Key=preview)
            if not head.get("Metadata", {}).get("nova-placeholder"):
                target_key = preview
            # placeholder → un-thumbnailable file; serve original, don't re-invoke
        except ClientError:
            _request_thumbnail(file.s3_key)  # backfill; serve original this once
    signed_url = _get_cloudfront_signed_url(target_key, expires_seconds=3600)
    return JsonResponse({
        "url": signed_url,
        "content_type": file.content_type,
        "name": file.name,
        "size": file.size_display(),
    })


@cognito_login_required
def view_file(request, pk):
    file = _require_file(pk, _get_owner_sub(request))
    if file.is_archived() and file.restore_status != DriveFile.RESTORE_READY:
        return render(request, "drive/archived.html", {"file": file})
    signed_url = _get_cloudfront_signed_url(file.s3_key, expires_seconds=3600)
    return redirect(signed_url)


@cognito_login_required
def file_thumbnail(request, pk):
    file = _require_file(pk, _get_owner_sub(request))
    max_age = 3600
    if file.content_type.startswith("image/"):
        # Pre-generated WebP thumb in STANDARD class — tiny, cheap, and
        # servable even while the original sits in Deep Archive.
        thumb = _thumb_key(file.s3_key)
        try:
            head = _s3().head_object(Bucket=settings.DRIVE_BUCKET_NAME, Key=thumb)
            if head.get("Metadata", {}).get("nova-placeholder"):
                # Known-unthumbable — icon fallback via onerror, no re-invoke
                return HttpResponse(status=404)
            signed_url = _get_cloudfront_signed_url(thumb, expires_seconds=3600)
        except ClientError:
            # No thumb yet (uploaded before the thumbnailer existed, or
            # generation still in flight) — queue one and serve the
            # original this time so the tile isn't blank.
            _request_thumbnail(file.s3_key)
            if file.is_archived() and file.restore_status != DriveFile.RESTORE_READY:
                return HttpResponse(status=404)
            signed_url = _get_cloudfront_signed_url(file.s3_key, expires_seconds=3600)
            max_age = 60  # re-check soon so the fresh thumb gets picked up
    elif file.content_type.startswith("video/"):
        if file.is_archived() and file.restore_status != DriveFile.RESTORE_READY:
            return HttpResponse(status=404)
        signed_url = _get_cloudfront_signed_url(file.s3_key, expires_seconds=3600)
    else:
        return HttpResponse(status=404)
    response = redirect(signed_url)
    response["Cache-Control"] = f"private, max-age={max_age}"
    return response


# ---------------------------------------------------------------------------
# Delete / restore / recycle bin
# ---------------------------------------------------------------------------

@cognito_login_required
@require_POST
def delete_file(request, pk):
    owner_sub = _get_owner_sub(request)
    file = _require_file(pk, owner_sub)
    if file.deleted_at:
        raise Http404
    dal.soft_delete_file(file.file_id)
    return HttpResponse("")


@cognito_login_required
@require_POST
def bulk_delete(request):
    try:
        data = json.loads(request.body)
        file_ids = data.get("ids", [])
        owner_sub = _get_owner_sub(request)
        deleted = []
        for fid in file_ids:
            f = dal.get_file(fid)
            if f and f.owner_sub == owner_sub and not f.deleted_at:
                dal.soft_delete_file(f.file_id)
                deleted.append(f.file_id)
        return JsonResponse({"deleted": deleted})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)


@cognito_login_required
@require_POST
def restore_from_bin(request, pk):
    owner_sub = _get_owner_sub(request)
    file = _require_file(pk, owner_sub)
    if not file.deleted_at:
        raise Http404
    dal.restore_file(file.file_id)
    file.deleted_at = None
    html = render(request, "drive/partials/recycle_row.html", {"file": file}).content.decode()
    return JsonResponse({"restored": True, "html": html})


@cognito_login_required
@require_POST
def restore_folder_from_bin(request, pk):
    owner_sub = _get_owner_sub(request)
    folder = _require_folder(pk, owner_sub)
    if not folder.deleted_at:
        raise Http404
    dal.restore_folder(folder.folder_id)
    return JsonResponse({"restored": True})


@cognito_login_required
@require_POST
def bulk_bin_restore(request):
    data = json.loads(request.body)
    file_ids = data.get("file_ids", [])
    folder_ids = data.get("folder_ids", [])
    owner_sub = _get_owner_sub(request)

    archived_ids = []
    for fid in file_ids:
        f = dal.get_file(fid)
        if f and f.owner_sub == owner_sub and f.deleted_at:
            if f.storage_class == DriveFile.DEEP_ARCHIVE:
                archived_ids.append(f.file_id)
            dal.restore_file(f.file_id)

    for fid in folder_ids:
        folder = dal.get_folder(fid)
        if folder and folder.owner_sub == owner_sub and folder.deleted_at:
            dal.restore_folder(folder.folder_id)

    return JsonResponse({
        "restored_files":     list(file_ids),
        "archived_file_ids":  archived_ids,
        "restored_folders":   list(folder_ids),
    })


@cognito_login_required
@require_POST
def bulk_bin_delete(request):
    data = json.loads(request.body)
    file_ids = data.get("file_ids", [])
    folder_ids = data.get("folder_ids", [])
    owner_sub = _get_owner_sub(request)
    s3 = _s3()

    deleted_file_ids = []
    for fid in file_ids:
        f = dal.get_file(fid)
        if f and f.owner_sub == owner_sub and f.deleted_at:
            _delete_object_and_thumb(s3, f.s3_key)
            dal.hard_delete_file(f.file_id)
            deleted_file_ids.append(f.file_id)

    deleted_folder_ids = []
    for fid in folder_ids:
        folder = dal.get_folder(fid)
        if not folder or folder.owner_sub != owner_sub or not folder.deleted_at:
            continue
        for subfolder_id in _collect_folder_ids(folder, owner_sub):
            for f in dal.list_files_in_folder(subfolder_id, active_only=False):
                _delete_object_and_thumb(s3, f.s3_key)
                dal.hard_delete_file(f.file_id)
            dal.hard_delete_folder(subfolder_id)
        deleted_folder_ids.append(folder.folder_id)

    return JsonResponse({"deleted_files": deleted_file_ids, "deleted_folders": deleted_folder_ids})


@cognito_login_required
@require_POST
def permanent_delete(request, pk):
    owner_sub = _get_owner_sub(request)
    file = _require_file(pk, owner_sub)
    if not file.deleted_at:
        raise Http404
    _delete_object_and_thumb(_s3(), file.s3_key)
    dal.hard_delete_file(file.file_id)
    return HttpResponse("")


@cognito_login_required
def recycle_bin(request):
    owner_sub = _get_owner_sub(request)
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=30)
    s3 = _s3()

    # Auto-permanently-delete files expired from the 30-day window
    all_files = dal.list_all_files(owner_sub)
    for f in all_files:
        if f.deleted_at and _parse_dt(f.deleted_at) < cutoff:
            _delete_object_and_thumb(s3, f.s3_key)
            dal.hard_delete_file(f.file_id)

    # Auto-permanently-delete expired folders and their contents
    all_folders = dal.list_all_folders(owner_sub)
    for folder in all_folders:
        if folder.deleted_at and _parse_dt(folder.deleted_at) < cutoff:
            for subfolder_id in _collect_folder_ids(folder, owner_sub):
                for f in dal.list_files_in_folder(subfolder_id, active_only=False):
                    _delete_object_and_thumb(s3, f.s3_key)
                    dal.hard_delete_file(f.file_id)
                dal.hard_delete_folder(subfolder_id)

    # Re-fetch after cleanup
    all_files = dal.list_all_files(owner_sub)
    all_folders = dal.list_all_folders(owner_sub)

    q = request.GET.get("q", "").strip()
    ql = q.lower()

    bin_files = [f for f in all_files if f.deleted_at]
    bin_folders = [fld for fld in all_folders if fld.deleted_at]
    if q:
        bin_files = [f for f in bin_files if ql in f.name.lower()]
        bin_folders = [fld for fld in bin_folders if ql in fld.name.lower()]

    for folder in bin_folders:
        folder_ids = _collect_folder_ids(folder, owner_sub)
        all_folder_files = []
        for fid in folder_ids:
            all_folder_files.extend(dal.list_files_in_folder(fid, active_only=False))
        folder.file_count = len([f for f in all_folder_files if not f.deleted_at])
        folder.subfolder_count = len(folder_ids) - 1

    ctx = {
        "files":          bin_files,
        "bin_folders":    bin_folders,
        "subfolders":     [],
        "current_folder": None,
        "breadcrumbs":    [],
        "is_recycle_bin": True,
        "view_mode":      _view_mode(request),
        "search_query":   q,
    }

    if request.headers.get("HX-Request"):
        return render(request, "drive/partials/search_results.html", ctx)

    active_files = [f for f in all_files if not f.deleted_at]
    ctx["sidebar_folders"] = _build_sidebar_tree(owner_sub)
    _, ctx["storage_used"], ctx["storage_pct"], ctx["total_files"] = _storage_stats(owner_sub)
    ctx["failed_count"] = _failed_upload_count(owner_sub)

    response = render(request, "drive/home.html", ctx)
    response["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# Failed upload history
# ---------------------------------------------------------------------------

# DynamoDB items are billed by size and the browser can hand us anything —
# cap the free-text fields before they're stored.
_MAX_ERROR_LEN    = 500
_MAX_FILENAME_LEN = 255


@cognito_login_required
@require_POST
def record_upload_failure(request):
    """Called by the browser when an upload fails at any stage.

    Never returns an error status the uploader has to handle — a failure to
    record a failure should stay silent rather than stack a second toast on
    top of the one the user is already looking at.
    """
    owner_sub = _get_owner_sub(request)
    try:
        data = json.loads(request.body)

        folder_id = None
        folder_name = ""
        if folder_pk := data.get("folder_pk"):
            folder = dal.get_folder(folder_pk)
            if folder and folder.owner_sub == owner_sub:
                folder_id = folder.folder_id
                folder_name = folder.name

        stage = data.get("stage", "unknown")
        if stage not in dict(UploadFailure.STAGE_CHOICES):
            stage = "unknown"

        failure = dal.create_upload_failure(
            owner_sub=owner_sub,
            filename=str(data.get("filename", ""))[:_MAX_FILENAME_LEN],
            size=int(data.get("size", 0) or 0),
            content_type=str(data.get("content_type", ""))[:100],
            folder_id=folder_id,
            folder_name=folder_name,
            stage=stage,
            error=str(data.get("error", ""))[:_MAX_ERROR_LEN],
        )
        return JsonResponse({"id": failure.failure_id})
    except Exception as e:
        logger.error("could not record upload failure: %s", e)
        return JsonResponse({"recorded": False}, status=200)


@cognito_login_required
def failed_uploads(request):
    owner_sub = _get_owner_sub(request)
    all_failures = dal.list_upload_failures(owner_sub)

    q = request.GET.get("q", "").strip()
    if q:
        ql = q.lower()
        failures = [
            f for f in all_failures
            if ql in f.filename.lower() or ql in f.error.lower()
        ]
    else:
        failures = all_failures

    ctx = {
        "failures":        failures,
        "files":           [],
        "subfolders":      [],
        "current_folder":  None,
        "breadcrumbs":     [],
        "is_failed_view":  True,
        "view_mode":      _view_mode(request),
        "search_query":    q,
    }

    if request.headers.get("HX-Request"):
        return render(request, "drive/partials/search_results.html", ctx)

    active_files = [f for f in dal.list_all_files(owner_sub) if not f.deleted_at]
    ctx["sidebar_folders"] = _build_sidebar_tree(owner_sub)
    _, ctx["storage_used"], ctx["storage_pct"], ctx["total_files"] = _storage_stats(owner_sub)
    ctx["failed_count"] = len(all_failures)   # already fetched — don't re-query

    response = render(request, "drive/home.html", ctx)
    response["Cache-Control"] = "no-store"
    return response


@cognito_login_required
@require_POST
def delete_upload_failures(request):
    """Delete selected failure records, or every record when clear_all is set."""
    owner_sub = _get_owner_sub(request)
    try:
        data = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        data = {}

    if data.get("clear_all"):
        target_ids = [f.failure_id for f in dal.list_upload_failures(owner_sub)]
    else:
        # Ownership check before deleting — ids come straight from the browser
        target_ids = [
            fid for fid in data.get("ids", [])
            if (rec := dal.get_upload_failure(fid)) and rec.owner_sub == owner_sub
        ]

    dal.delete_upload_failures(target_ids)
    return JsonResponse({"deleted": target_ids, "remaining": _failed_upload_count(owner_sub)})


# ---------------------------------------------------------------------------
# Archive / Glacier
# ---------------------------------------------------------------------------

@cognito_login_required
@require_POST
def archive_files(request):
    try:
        data = json.loads(request.body)
        file_ids = data.get("ids", [])
        owner_sub = _get_owner_sub(request)

        updated_html = []
        archived_names = []
        for fid in file_ids:
            f = dal.get_file(fid)
            if not f or f.owner_sub != owner_sub:
                continue
            _s3().copy_object(
                Bucket=settings.DRIVE_BUCKET_NAME,
                CopySource={"Bucket": settings.DRIVE_BUCKET_NAME, "Key": f.s3_key},
                Key=f.s3_key,
                StorageClass="DEEP_ARCHIVE",
                MetadataDirective="COPY",
            )
            dal.update_file_storage_class(f.file_id, "DEEP_ARCHIVE")
            f.storage_class = "DEEP_ARCHIVE"
            html = render(request, "drive/partials/file_row.html", {"file": f}).content.decode()
            updated_html.append({"id": f.file_id, "html": html})
            archived_names.append(f.name)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)

    user_email = request.session.get("user_email", "")
    if user_email and archived_names:
        try:
            _send_archive_email(user_email, archived_names)
        except Exception as email_err:
            logger.error("archive email failed: %s", email_err, exc_info=True)

    return JsonResponse({"updated": updated_html})


@cognito_login_required
def archive_view(request):
    owner_sub = _get_owner_sub(request)
    q = request.GET.get("q", "").strip()

    all_files = dal.list_all_files(owner_sub)
    archived_files = [
        f for f in all_files
        if not f.deleted_at
        and f.storage_class == DriveFile.DEEP_ARCHIVE
        and f.restore_status != DriveFile.RESTORE_READY
    ]
    if q:
        ql = q.lower()
        archived_files = [f for f in archived_files if ql in f.name.lower()]

    ctx = {
        "files":          archived_files,
        "subfolders":     [],
        "current_folder": None,
        "breadcrumbs":    [],
        "is_archive_view": True,
        "view_mode":      _view_mode(request),
        "search_query":   q,
    }

    if request.headers.get("HX-Request"):
        return render(request, "drive/partials/search_results.html", ctx)

    active_files = [f for f in all_files if not f.deleted_at]
    ctx["sidebar_folders"] = _build_sidebar_tree(owner_sub)
    _, ctx["storage_used"], ctx["storage_pct"], ctx["total_files"] = _storage_stats(owner_sub)
    ctx["failed_count"] = _failed_upload_count(owner_sub)

    response = render(request, "drive/home.html", ctx)
    response["Cache-Control"] = "no-store"
    return response


@cognito_login_required
@require_POST
def bulk_restore(request):
    data = json.loads(request.body)
    file_ids = data.get("ids", [])
    owner_sub = _get_owner_sub(request)
    user_email = request.session.get("user_email", "")

    updated_html = []
    restored_names = []
    for fid in file_ids:
        f = dal.get_file(fid)
        if (not f or f.owner_sub != owner_sub
                or f.storage_class != DriveFile.DEEP_ARCHIVE
                or f.restore_status
                or f.deleted_at):
            continue
        try:
            _s3().restore_object(
                Bucket=settings.DRIVE_BUCKET_NAME,
                Key=f.s3_key,
                RestoreRequest={"Days": 7, "GlacierJobParameters": {"Tier": "Standard"}},
            )
        except ClientError as e:
            code = e.response["Error"]["Code"]
            logger.error("bulk restore_object failed file=%s code=%s", f.file_id, code)
            if code != "RestoreAlreadyInProgress":
                continue

        dal.update_file_restore(f.file_id, DriveFile.RESTORE_PENDING, notify_email=user_email)
        f.restore_status = DriveFile.RESTORE_PENDING
        html = render(request, "drive/partials/file_row.html", {"file": f}).content.decode()
        updated_html.append({"id": f.file_id, "html": html})
        restored_names.append(f.name)

    if user_email and restored_names:
        try:
            _send_bulk_restore_email(user_email, restored_names)
        except Exception as email_err:
            logger.error("bulk restore email failed: %s", email_err, exc_info=True)

    return JsonResponse({"updated": updated_html})


@cognito_login_required
@require_POST
def restore_file(request, pk):
    owner_sub = _get_owner_sub(request)
    file = _require_file(pk, owner_sub)

    if not file.is_archived():
        return JsonResponse({"error": "File is not archived"}, status=400)
    if file.restore_status == DriveFile.RESTORE_PENDING:
        return JsonResponse({"error": "Restore already in progress"}, status=400)

    user_email = request.session.get("user_email", "")
    try:
        _s3().restore_object(
            Bucket=settings.DRIVE_BUCKET_NAME,
            Key=file.s3_key,
            RestoreRequest={"Days": 7, "GlacierJobParameters": {"Tier": "Standard"}},
        )
    except ClientError as e:
        code = e.response["Error"]["Code"]
        logger.error("restore_object failed code=%s err=%s", code, e)
        if code != "RestoreAlreadyInProgress":
            return JsonResponse({"error": str(e)}, status=400)

    dal.update_file_restore(file.file_id, DriveFile.RESTORE_PENDING, notify_email=user_email)
    file.restore_status = DriveFile.RESTORE_PENDING

    if user_email:
        try:
            _send_restore_started_email(user_email, file.name)
        except Exception as email_err:
            logger.error("restore email failed: %s", email_err, exc_info=True)

    html = render(request, "drive/partials/file_row.html", {"file": file}).content.decode()
    return HttpResponse(html, content_type="text/html")


# ---------------------------------------------------------------------------
# Email helpers (Resend — restore / archive notifications only)
# ---------------------------------------------------------------------------

def _send_bulk_restore_email(to_email, file_names):
    resend.api_key = _get_resend_api_key()
    count = len(file_names)
    noun = "file" if count == 1 else "files"
    file_list_html = "".join(
        f'<li style="padding:4px 0;color:#cbd5e1;">{name}</li>' for name in file_names
    )
    html_body = f"""
    <div style="font-family:sans-serif;max-width:560px;margin:0 auto;background:#0f172a;padding:32px;border-radius:12px;">
        <h2 style="color:#f1f5f9;margin-top:0;">NovaDrive — Restore Started</h2>
        <p style="color:#94a3b8;">{count} {noun} are being restored from Glacier Deep Archive:</p>
        <ul style="background:#1e293b;border-radius:8px;padding:16px 16px 16px 32px;margin:16px 0;">
            {file_list_html}
        </ul>
        <p style="color:#94a3b8;">
            Retrieval typically takes <strong style="color:#f1f5f9;">12–48 hours</strong>.
            We'll send you another email as soon as your {noun} {"is" if count == 1 else "are"} ready.
        </p>
        <hr style="border:none;border-top:1px solid #1e293b;margin:24px 0;">
        <p style="color:#475569;font-size:12px;margin:0;">NovaDrive &nbsp;·&nbsp; nodepulsecaringal.xyz</p>
    </div>
    """
    resend.Emails.send({
        "from": settings.DRIVE_FROM_EMAIL,
        "to": [to_email],
        "subject": f"NovaDrive: Restoring {count} {noun} — we'll notify you when ready",
        "html": html_body,
    })


def _send_restore_started_email(to_email, file_name):
    resend.api_key = _get_resend_api_key()
    html_body = f"""
    <div style="font-family:sans-serif;max-width:560px;margin:0 auto;background:#0f172a;padding:32px;border-radius:12px;">
        <h2 style="color:#f1f5f9;margin-top:0;">NovaDrive — Restore Started</h2>
        <p style="color:#94a3b8;">Your file is being restored from Glacier Deep Archive:</p>
        <div style="background:#1e293b;border-radius:8px;padding:16px;margin:16px 0;border-left:4px solid #a78bfa;">
            <p style="color:#e2e8f0;margin:0;font-weight:600;">{file_name}</p>
        </div>
        <p style="color:#94a3b8;">
            Glacier Deep Archive retrieval typically takes <strong style="color:#f1f5f9;">12–48 hours</strong>.
            We'll send you another email as soon as your file is ready.
        </p>
        <hr style="border:none;border-top:1px solid #1e293b;margin:24px 0;">
        <p style="color:#475569;font-size:12px;margin:0;">NovaDrive &nbsp;·&nbsp; nodepulsecaringal.xyz</p>
    </div>
    """
    resend.Emails.send({
        "from": settings.DRIVE_FROM_EMAIL,
        "to": [to_email],
        "subject": f'NovaDrive: Restoring "{file_name}" — we\'ll notify you when ready',
        "html": html_body,
    })


def _send_archive_email(to_email, file_names):
    resend.api_key = _get_resend_api_key()
    count = len(file_names)
    noun = "file" if count == 1 else "files"
    file_list_html = "".join(
        f'<li style="padding:4px 0;color:#cbd5e1;">{name}</li>' for name in file_names
    )
    html_body = f"""
    <div style="font-family:sans-serif;max-width:560px;margin:0 auto;background:#0f172a;padding:32px;border-radius:12px;">
        <h2 style="color:#f1f5f9;margin-top:0;">NovaDrive — Archive Confirmation</h2>
        <p style="color:#94a3b8;">{count} {noun} have been moved to <strong style="color:#a78bfa;">Glacier Deep Archive</strong>.</p>
        <ul style="background:#1e293b;border-radius:8px;padding:16px 16px 16px 32px;margin:16px 0;">
            {file_list_html}
        </ul>
        <p style="color:#64748b;font-size:13px;">
            Archived files cannot be previewed or downloaded directly.
            To restore them, initiate a Glacier restore request (retrieval: 12–48 hours).
        </p>
        <hr style="border:none;border-top:1px solid #1e293b;margin:24px 0;">
        <p style="color:#475569;font-size:12px;margin:0;">NovaDrive &nbsp;·&nbsp; nodepulsecaringal.xyz</p>
    </div>
    """
    resend.Emails.send({
        "from": settings.DRIVE_FROM_EMAIL,
        "to": [to_email],
        "subject": f"NovaDrive: {count} {noun} archived to Glacier Deep Archive",
        "html": html_body,
    })


# ---------------------------------------------------------------------------
# Batch zip-folder download
# ---------------------------------------------------------------------------

@cognito_login_required
@require_POST
def zip_folder(request, pk=None):
    owner_sub = _get_owner_sub(request)
    bucket = settings.DRIVE_BUCKET_NAME

    try:
        body = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        body = {}

    folder_ids = body.get("folder_ids") or ([pk] if pk else [])
    if not folder_ids:
        return JsonResponse({"error": "No folders specified"}, status=400)

    folders = [dal.get_folder(fid) for fid in folder_ids]
    folders = [f for f in folders if f and f.owner_sub == owner_sub and not f.deleted_at]
    if len(folders) != len(folder_ids):
        return JsonResponse({"error": "One or more folders not found"}, status=404)

    batch_client = boto3.client("batch", region_name=settings.AWS_REGION)
    submitted = []

    for folder in folders:
        batch_job = dal.create_batch_job(owner_sub, "zip_folder", folder.name)
        try:
            response = batch_client.submit_job(
                jobName=f"zip-folder-{folder.folder_id[:8]}-{batch_job.job_id[:8]}",
                jobQueue=settings.BATCH_JOB_QUEUE,
                jobDefinition=settings.BATCH_JOB_DEFINITION,
                containerOverrides={
                    "environment": [
                        {"name": "JOB_TYPE",                "value": "zip_folder"},
                        {"name": "FOLDER_IDS",              "value": folder.folder_id},
                        {"name": "OWNER_SUB",               "value": owner_sub},
                        {"name": "JOB_DB_ID",               "value": batch_job.job_id},
                        {"name": "DRIVE_BUCKET_NAME",       "value": bucket},
                        {"name": "AWS_REGION",              "value": settings.AWS_REGION},
                        {"name": "DYNAMODB_FOLDERS_TABLE",  "value": os.environ.get("DYNAMODB_FOLDERS_TABLE", "")},
                        {"name": "DYNAMODB_FILES_TABLE",    "value": os.environ.get("DYNAMODB_FILES_TABLE", "")},
                        {"name": "DYNAMODB_BATCH_JOBS_TABLE", "value": os.environ.get("DYNAMODB_BATCH_JOBS_TABLE", "")},
                    ]
                },
            )
            dal.set_batch_job_aws_id(batch_job.job_id, response["jobId"])
            submitted.append({"job_id": batch_job.job_id, "folder_name": folder.name})
        except Exception as e:
            logger.error("batch submit failed folder=%s: %s", folder.folder_id, e)
            dal.set_batch_job_failed(batch_job.job_id)
            submitted.append({"job_id": batch_job.job_id, "folder_name": folder.name, "error": str(e)})

    return JsonResponse({"status": "pending", "jobs": submitted})


@cognito_login_required
def job_status(request, job_id):
    owner_sub = _get_owner_sub(request)
    job = dal.get_batch_job(job_id)
    if not job or job.owner_sub != owner_sub:
        raise Http404

    if job.status == BatchJob.READY:
        url = _s3().generate_presigned_url(
            "get_object",
            Params={
                "Bucket": settings.DRIVE_BUCKET_NAME,
                "Key": job.result_key,
                "ResponseContentDisposition": f'attachment; filename="{job.folder_name}.zip"',
            },
            ExpiresIn=3600,
        )
        return JsonResponse({"status": "ready", "progress": 100, "url": url})

    return JsonResponse({"status": job.status, "progress": job.progress})


# ---------------------------------------------------------------------------
# Timeline (Photos view)
# ---------------------------------------------------------------------------

@cognito_login_required
def timeline_view(request):
    owner_sub = _get_owner_sub(request)

    all_files = dal.list_all_files(owner_sub)
    media = [
        f for f in all_files
        if not f.deleted_at
        and (f.content_type.startswith("image/") or f.content_type.startswith("video/"))
        and (f.storage_class == DriveFile.GLACIER_IR
             or (f.storage_class == DriveFile.DEEP_ARCHIVE
                 and f.restore_status == DriveFile.RESTORE_READY))
    ]
    media.sort(
        key=lambda f: f.effective_date or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc),
        reverse=True,
    )

    def _date_key(f):
        return f.effective_date.date() if f.effective_date else datetime.date.min

    groups = [
        {"date": date, "files": list(files)}
        for date, files in itertools.groupby(media, key=_date_key)
    ]

    active_files = [f for f in all_files if not f.deleted_at]
    _, storage_used, storage_pct, _ = _storage_stats(owner_sub)

    ctx = {
        "groups":         groups,
        "total_media":    len(media),
        "is_timeline_view": True,
        "sidebar_folders": _build_sidebar_tree(owner_sub),
        "storage_used":   storage_used,
        "storage_pct":    storage_pct,
        "total_files":    len(active_files),
        "failed_count":   _failed_upload_count(owner_sub),
        "files":          active_files,
        "subfolders":     [],
        "current_folder": None,
        "breadcrumbs":    [],
        "search_query":   "",
    }

    response = render(request, "drive/home.html", ctx)
    response["Cache-Control"] = "no-store"
    return response
