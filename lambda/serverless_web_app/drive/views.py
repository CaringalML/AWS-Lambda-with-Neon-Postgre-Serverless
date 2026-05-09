import datetime
import json
import logging
import os
import uuid
from urllib.parse import quote
from django.db.models import Sum

logger = logging.getLogger(__name__)

import resend

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.signers import CloudFrontSigner
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from django.conf import settings
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.views.decorators.http import require_POST

from accounts.decorators import cognito_login_required
from .models import DriveFile, DriveFolder, BatchJob


# ---------------------------------------------------------------------------
# Module-level cache — survives across Lambda invocations in the same container
# ---------------------------------------------------------------------------
_cf_private_key_cache = None


def _get_folder_path(folder_pk, owner_sub):
    """Walk the folder parent chain and return a safe S3 path string.

    e.g. folder 'vacation' inside 'photos' → 'photos/vacation'
    Root uploads (folder_pk is None) return None (no extra path segment).
    """
    if not folder_pk:
        return None
    parts = []
    visited = set()
    try:
        folder = DriveFolder.objects.get(pk=folder_pk, owner_sub=owner_sub)
        while folder and folder.pk not in visited:
            visited.add(folder.pk)
            safe = "".join(
                c if c.isalnum() or c in "-_. " else "_" for c in folder.name
            ).strip() or "_"
            parts.insert(0, safe)
            folder = folder.parent
    except DriveFolder.DoesNotExist:
        pass
    return "/".join(parts) if parts else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_resend_api_key():
    """Fetch Resend API key fresh from SSM each time — avoids cold-start caching."""
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
    """Return the Cognito sub stored in the session."""
    return request.session.get("user_sub", "")


def _get_cloudfront_signed_url(s3_key, expires_seconds=300):
    """Generate a CloudFront signed URL valid for expires_seconds.
    Private key is cached in the module-level global so SSM is only hit
    once per Lambda container (cold start), not on every request."""
    global _cf_private_key_cache
    if _cf_private_key_cache is None:
        ssm = boto3.client("ssm", region_name=settings.AWS_REGION)
        pem = ssm.get_parameter(
            Name=settings.CLOUDFRONT_PRIVATE_KEY_SSM_NAME,
            WithDecryption=True,
        )["Parameter"]["Value"]
        _cf_private_key_cache = serialization.load_pem_private_key(pem.encode(), password=None)

    private_key = _cf_private_key_cache

    def rsa_signer(message):
        return private_key.sign(message, padding.PKCS1v15(), hashes.SHA1())

    cf_signer = CloudFrontSigner(settings.CLOUDFRONT_KEY_PAIR_ID, rsa_signer)
    # URL-encode the path (spaces → %20 etc.) so the signature matches what
    # the browser actually requests. safe='/' preserves path separators.
    encoded_key = quote(s3_key, safe="/")
    url = f"https://{settings.CLOUDFRONT_DOMAIN}/{encoded_key}"
    expire_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=expires_seconds)
    return cf_signer.generate_presigned_url(url, date_less_than=expire_at)


def _build_breadcrumbs(folder):
    """Walk up the parent chain and return [root, ..., folder].
    visited set guards against circular parent references in corrupt data."""
    crumbs = []
    visited = set()
    node = folder
    while node and node.pk not in visited:
        visited.add(node.pk)
        crumbs.insert(0, node)
        node = node.parent
    return crumbs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STORAGE_CAP_BYTES = 15 * 1024 ** 3  # 15 GB display cap for the bar

def _storage_stats(owner_sub):
    """Return (used_bytes, used_display, pct) for all non-deleted files owned by owner_sub."""
    result = DriveFile.objects.filter(
        owner_sub=owner_sub, deleted_at__isnull=True
    ).aggregate(total=Sum('size'))
    raw = result['total'] or 0
    pct = min(100, round(raw / _STORAGE_CAP_BYTES * 100, 1))
    total = float(raw)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if total < 1024:
            display = f"{total:.1f} {unit}"
            return raw, display, pct
        total /= 1024
    return raw, f"{total:.1f} PB", pct


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

@cognito_login_required
def drive_home(request, folder_pk=None):
    owner_sub = _get_owner_sub(request)
    current_folder = None
    breadcrumbs = []
    if folder_pk:
        current_folder = get_object_or_404(DriveFolder, pk=folder_pk, owner_sub=owner_sub)
        breadcrumbs = _build_breadcrumbs(current_folder)

    from django.db.models import Q, Prefetch
    now = datetime.datetime.now(datetime.timezone.utc)

    # Expire any restored files whose 7-day window has passed — reset back to archived
    DriveFile.objects.filter(
        owner_sub=owner_sub,
        restore_status=DriveFile.RESTORE_READY,
        restore_expires_at__isnull=False,
        restore_expires_at__lt=now,
    ).update(restore_status="", restore_expires_at=None)

    q = request.GET.get("q", "").strip()

    if q:
        # Global search — span all folders so users can find any file by name
        files = DriveFile.objects.filter(
            owner_sub=owner_sub,
            deleted_at__isnull=True,
            name__icontains=q,
        ).filter(
            Q(storage_class=DriveFile.GLACIER_IR)
            | Q(storage_class=DriveFile.DEEP_ARCHIVE, restore_status=DriveFile.RESTORE_READY)
        )
        subfolders = DriveFolder.objects.filter(
            owner_sub=owner_sub,
            deleted_at__isnull=True,
            name__icontains=q,
        )
    else:
        # Normal folder-scoped listing
        files = DriveFile.objects.filter(
            owner_sub=owner_sub,
            folder=current_folder,
            deleted_at__isnull=True,
        ).filter(
            # Show instantly-accessible files, or Deep Archive / Glacier files that have been restored
            Q(storage_class=DriveFile.GLACIER_IR)
            | Q(storage_class=DriveFile.DEEP_ARCHIVE, restore_status=DriveFile.RESTORE_READY)
        )
        subfolders = DriveFolder.objects.filter(owner_sub=owner_sub, parent=current_folder, deleted_at__isnull=True)

    ctx = {
        "files": files,
        "subfolders": subfolders,
        "current_folder": current_folder,
        "breadcrumbs": breadcrumbs,
        "search_query": q,
    }

    if request.headers.get("HX-Request"):
        return render(request, "drive/partials/search_results.html", ctx)

    active_subfolders = DriveFolder.objects.filter(deleted_at__isnull=True)
    ctx["sidebar_folders"] = DriveFolder.objects.filter(
        owner_sub=owner_sub, parent=None, deleted_at__isnull=True
    ).prefetch_related(
        Prefetch('subfolders', queryset=active_subfolders),
        Prefetch('subfolders__subfolders', queryset=active_subfolders),
        Prefetch('subfolders__subfolders__subfolders', queryset=active_subfolders),
    )
    _, ctx["storage_used"], ctx["storage_pct"] = _storage_stats(owner_sub)
    ctx["total_files"] = DriveFile.objects.filter(owner_sub=owner_sub, deleted_at__isnull=True).count()

    response = render(request, "drive/home.html", ctx)
    response["Cache-Control"] = "no-store"
    return response


@cognito_login_required
@require_POST
def create_folder(request):
    """Create a new folder and return its rendered row HTML."""
    try:
        data = json.loads(request.body)
        name = data.get("name", "").strip()
        parent_pk = data.get("parent_pk")
        owner_sub = _get_owner_sub(request)

        if not name:
            return JsonResponse({"error": "Folder name is required"}, status=400)

        parent = None
        if parent_pk:
            parent = get_object_or_404(DriveFolder, pk=parent_pk, owner_sub=owner_sub)

        folder = DriveFolder.objects.create(
            owner_sub=owner_sub,
            name=name,
            parent=parent,
        )

        html = render(request, "drive/partials/folder_row.html", {"folder": folder}).content.decode()
        return JsonResponse({"html": html, "id": folder.id})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)


def _collect_folder_ids(root):
    """Iteratively walk the folder subtree and return a list of all folder PKs."""
    ids = []
    queue = [root]
    visited = set()
    while queue:
        f = queue.pop()
        if f.pk in visited:
            continue
        visited.add(f.pk)
        ids.append(f.pk)
        queue.extend(f.subfolders.all())
    return ids


@cognito_login_required
@require_POST
def delete_folder(request, pk):
    """Soft-delete a folder — moves it to Recycle Bin. Files inside stay linked to the folder."""
    owner_sub = _get_owner_sub(request)
    folder = get_object_or_404(DriveFolder, pk=pk, owner_sub=owner_sub, deleted_at__isnull=True)
    folder.deleted_at = datetime.datetime.now(datetime.timezone.utc)
    folder.save(update_fields=['deleted_at'])
    return HttpResponse("")


def _s3_move(s3, old_key, new_key):
    """Copy an S3 object to a new key then delete the original."""
    s3.copy_object(
        Bucket=settings.DRIVE_BUCKET_NAME,
        CopySource={"Bucket": settings.DRIVE_BUCKET_NAME, "Key": old_key},
        Key=new_key,
        MetadataDirective="COPY",
    )
    s3.delete_object(Bucket=settings.DRIVE_BUCKET_NAME, Key=old_key)


@cognito_login_required
@require_POST
def rename_folder(request, pk):
    """Rename a folder and move all files in its subtree to new S3 keys."""
    owner_sub = _get_owner_sub(request)
    folder = get_object_or_404(DriveFolder, pk=pk, owner_sub=owner_sub, deleted_at__isnull=True)
    try:
        data = json.loads(request.body)
        name = data.get("name", "").strip()
        if not name:
            return JsonResponse({"error": "Name cannot be empty."}, status=400)

        # Compute old path BEFORE renaming
        old_path = _get_folder_path(pk, owner_sub)  # e.g. "photos/vacation"
        old_prefix = f"{owner_sub}/{old_path}/" if old_path else f"{owner_sub}/"

        # Rename in DB
        folder.name = name
        folder.save(update_fields=["name"])

        # Compute new path AFTER renaming
        new_path = _get_folder_path(pk, owner_sub)
        new_prefix = f"{owner_sub}/{new_path}/" if new_path else f"{owner_sub}/"

        # Move all files in the subtree to new S3 keys
        if old_prefix != new_prefix:
            s3 = _s3()
            all_folder_ids = _collect_folder_ids(folder)
            files = DriveFile.objects.filter(folder_id__in=all_folder_ids, owner_sub=owner_sub)
            for f in files:
                if f.s3_key.startswith(old_prefix):
                    new_key = new_prefix + f.s3_key[len(old_prefix):]
                    try:
                        _s3_move(s3, f.s3_key, new_key)
                        f.s3_key = new_key
                        f.save(update_fields=["s3_key"])
                    except ClientError as e:
                        logger.error("S3 move failed %s → %s: %s", f.s3_key, new_key, e)

        return JsonResponse({"id": folder.pk, "name": folder.name})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)


@cognito_login_required
@require_POST
def rename_file(request, pk):
    """Rename a file and move it to the new S3 key."""
    owner_sub = _get_owner_sub(request)
    file = get_object_or_404(DriveFile, pk=pk, owner_sub=owner_sub, deleted_at__isnull=True)
    try:
        data = json.loads(request.body)
        name = data.get("name", "").strip()
        if not name:
            return JsonResponse({"error": "Name cannot be empty."}, status=400)

        # Build new S3 key — same directory, new filename
        old_key = file.s3_key
        directory = old_key.rsplit("/", 1)[0] if "/" in old_key else owner_sub
        new_key = f"{directory}/{name}"

        if old_key != new_key:
            try:
                _s3_move(_s3(), old_key, new_key)
                file.s3_key = new_key
            except ClientError as e:
                logger.error("S3 move failed %s → %s: %s", old_key, new_key, e)

        file.name = name
        file.save(update_fields=["name", "s3_key"])
        return JsonResponse({"id": file.pk, "name": file.name})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)


@cognito_login_required
@require_POST
def upload_url(request):
    """Return a presigned S3 POST URL — the browser uploads directly to S3."""
    try:
        if not settings.DRIVE_BUCKET_NAME:
            return JsonResponse({"error": "DRIVE_BUCKET_NAME env var not set — has Terraform been applied?"}, status=500)

        data = json.loads(request.body)
        filename     = data.get("filename", "unnamed")
        content_type = data.get("content_type", "application/octet-stream")
        owner_sub    = _get_owner_sub(request)
        folder_pk    = data.get("folder_pk")

        folder_path = _get_folder_path(folder_pk, owner_sub)
        if folder_path:
            s3_key = f"{owner_sub}/{folder_path}/{filename}"
        else:
            s3_key = f"{owner_sub}/{filename}"

        exists = DriveFile.objects.filter(
            s3_key=s3_key, owner_sub=owner_sub, deleted_at__isnull=True
        ).exists()

        presigned = _s3().generate_presigned_post(
            Bucket=settings.DRIVE_BUCKET_NAME,
            Key=s3_key,
            Fields={"Content-Type": content_type},
            Conditions=[
                {"Content-Type": content_type},
                ["content-length-range", 1, 500 * 1024 * 1024],  # max 500 MB
            ],
            ExpiresIn=300,
        )
        return JsonResponse({"url": presigned["url"], "fields": presigned["fields"], "s3_key": s3_key, "exists": exists})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)


@cognito_login_required
@require_POST
def confirm_upload(request):
    """Save file metadata after successful S3 upload."""
    try:
        data = json.loads(request.body)
        owner_sub = _get_owner_sub(request)
        folder_pk = data.get("folder_pk")

        folder = None
        if folder_pk:
            folder = get_object_or_404(DriveFolder, pk=folder_pk, owner_sub=owner_sub)

        # Verify the object actually exists in S3
        head = _s3().head_object(
            Bucket=settings.DRIVE_BUCKET_NAME,
            Key=data["s3_key"],
        )

        captured_at = None
        captured_at_str = data.get("captured_at")
        if captured_at_str:
            try:
                captured_at = datetime.datetime.fromisoformat(captured_at_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass

        drive_file, created = DriveFile.objects.update_or_create(
            s3_key=data["s3_key"],
            defaults={
                "owner_sub": owner_sub,
                "name": data["filename"],
                "size": head["ContentLength"],
                "content_type": head.get("ContentType", "application/octet-stream"),
                "folder": folder,
                "storage_class": DriveFile.GLACIER_IR,
                "deleted_at": None,
                "restore_status": "",
                "restore_expires_at": None,
                "captured_at": captured_at,
            },
        )

        _s3().copy_object(
            Bucket=settings.DRIVE_BUCKET_NAME,
            CopySource={"Bucket": settings.DRIVE_BUCKET_NAME, "Key": data["s3_key"]},
            Key=data["s3_key"],
            StorageClass=DriveFile.GLACIER_IR,
            MetadataDirective="COPY",
        )

        html = render(request, "drive/partials/file_row.html", {"file": drive_file}).content.decode()
        _, storage_used, _ = _storage_stats(owner_sub)
        return JsonResponse({"html": html, "id": drive_file.id, "storage_used": storage_used, "overwritten": not created})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)


@cognito_login_required
def download_file(request, pk):
    """Redirect to a presigned S3 GET URL with Content-Disposition: attachment."""
    file = get_object_or_404(DriveFile, pk=pk, owner_sub=_get_owner_sub(request))
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
    """Return a signed CloudFront URL as JSON for the in-browser preview modal."""
    file = get_object_or_404(DriveFile, pk=pk, owner_sub=_get_owner_sub(request))
    if file.is_archived() and file.restore_status != DriveFile.RESTORE_READY:
        return JsonResponse(
            {"error": "archived", "message": "This file is archived and cannot be previewed."},
            status=400,
        )
    signed_url = _get_cloudfront_signed_url(file.s3_key, expires_seconds=3600)
    return JsonResponse({
        "url": signed_url,
        "content_type": file.content_type,
        "name": file.name,
        "size": file.size_display(),
    })


@cognito_login_required
def view_file(request, pk):
    """Redirect to a short-lived CloudFront signed URL (fallback / direct link)."""
    file = get_object_or_404(DriveFile, pk=pk, owner_sub=_get_owner_sub(request))

    if file.is_archived() and file.restore_status != DriveFile.RESTORE_READY:
        return render(request, "drive/archived.html", {"file": file})

    signed_url = _get_cloudfront_signed_url(file.s3_key, expires_seconds=3600)
    return redirect(signed_url)


@cognito_login_required
def file_thumbnail(request, pk):
    """Redirect to a 1-hour CloudFront signed URL for image/video thumbnails.
    Browser caches the redirect for 1 hour so Lambda is only hit once per file
    per session. Only serves image/* and video/* — returns 404 for everything else."""
    file = get_object_or_404(DriveFile, pk=pk, owner_sub=_get_owner_sub(request))
    if not (file.content_type.startswith("image/") or file.content_type.startswith("video/")):
        return HttpResponse(status=404)
    if file.is_archived() and file.restore_status != DriveFile.RESTORE_READY:
        return HttpResponse(status=404)
    signed_url = _get_cloudfront_signed_url(file.s3_key, expires_seconds=3600)
    response = redirect(signed_url)
    response["Cache-Control"] = "private, max-age=3600"
    return response


@cognito_login_required
@require_POST
def delete_file(request, pk):
    """Soft-delete: move to Recycle Bin. Permanent deletion happens after 30 days."""
    file = get_object_or_404(DriveFile, pk=pk, owner_sub=_get_owner_sub(request), deleted_at__isnull=True)
    file.deleted_at = datetime.datetime.now(datetime.timezone.utc)
    file.save(update_fields=["deleted_at"])
    return HttpResponse("")


@cognito_login_required
@require_POST
def bulk_delete(request):
    """Soft-delete multiple files at once."""
    try:
        data = json.loads(request.body)
        file_ids = data.get("ids", [])
        owner_sub = _get_owner_sub(request)
        now = datetime.datetime.now(datetime.timezone.utc)
        deleted_ids = list(
            DriveFile.objects.filter(
                id__in=file_ids, owner_sub=owner_sub, deleted_at__isnull=True
            ).values_list("id", flat=True)
        )
        DriveFile.objects.filter(id__in=deleted_ids).update(deleted_at=now)
        return JsonResponse({"deleted": deleted_ids})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)


@cognito_login_required
@require_POST
def restore_from_bin(request, pk):
    """Restore a file from the Recycle Bin back to My Drive."""
    file = get_object_or_404(DriveFile, pk=pk, owner_sub=_get_owner_sub(request), deleted_at__isnull=False)
    file.deleted_at = None
    file.save(update_fields=["deleted_at"])
    html = render(request, "drive/partials/recycle_row.html", {"file": file}).content.decode()
    return JsonResponse({"restored": True, "html": html})


@cognito_login_required
@require_POST
def restore_folder_from_bin(request, pk):
    """Restore a folder from the Recycle Bin back to My Drive."""
    folder = get_object_or_404(DriveFolder, pk=pk, owner_sub=_get_owner_sub(request), deleted_at__isnull=False)
    folder.deleted_at = None
    folder.save(update_fields=['deleted_at'])
    return JsonResponse({"restored": True})


@cognito_login_required
@require_POST
def bulk_bin_restore(request):
    """Restore multiple files and/or folders from the Recycle Bin back to My Drive."""
    data = json.loads(request.body)
    file_ids = data.get("file_ids", [])
    folder_ids = data.get("folder_ids", [])
    owner_sub = _get_owner_sub(request)
    files = list(DriveFile.objects.filter(pk__in=file_ids, owner_sub=owner_sub, deleted_at__isnull=False))
    archived_ids = [f.pk for f in files if f.storage_class == DriveFile.DEEP_ARCHIVE]
    DriveFile.objects.filter(pk__in=file_ids, owner_sub=owner_sub, deleted_at__isnull=False).update(deleted_at=None)
    DriveFolder.objects.filter(pk__in=folder_ids, owner_sub=owner_sub, deleted_at__isnull=False).update(deleted_at=None)
    return JsonResponse({
        "restored_files": list(file_ids),
        "archived_file_ids": archived_ids,
        "restored_folders": list(folder_ids),
    })


@cognito_login_required
@require_POST
def bulk_bin_delete(request):
    """Permanently delete multiple files and/or folders from S3 and DB."""
    data = json.loads(request.body)
    file_ids = data.get("file_ids", [])
    folder_ids = data.get("folder_ids", [])
    owner_sub = _get_owner_sub(request)
    s3 = _s3()

    deleted_file_ids = []
    files = list(DriveFile.objects.filter(pk__in=file_ids, owner_sub=owner_sub, deleted_at__isnull=False))
    for f in files:
        pk = f.pk
        try:
            s3.delete_object(Bucket=settings.DRIVE_BUCKET_NAME, Key=f.s3_key)
        except ClientError:
            pass
        f.delete()
        deleted_file_ids.append(pk)

    deleted_folder_ids = []
    folders = list(DriveFolder.objects.filter(pk__in=folder_ids, owner_sub=owner_sub, deleted_at__isnull=False))
    for folder in folders:
        fid = folder.pk
        all_folder_ids = _collect_folder_ids(folder)
        files_inside = DriveFile.objects.filter(folder_id__in=all_folder_ids, owner_sub=owner_sub)
        for f in files_inside:
            try:
                s3.delete_object(Bucket=settings.DRIVE_BUCKET_NAME, Key=f.s3_key)
            except ClientError:
                pass
        files_inside.delete()
        folder.delete()  # CASCADE removes subfolders
        deleted_folder_ids.append(fid)

    return JsonResponse({"deleted_files": deleted_file_ids, "deleted_folders": deleted_folder_ids})


@cognito_login_required
@require_POST
def permanent_delete(request, pk):
    """Permanently delete a file from S3 and DB — no recovery possible."""
    file = get_object_or_404(DriveFile, pk=pk, owner_sub=_get_owner_sub(request), deleted_at__isnull=False)
    try:
        _s3().delete_object(Bucket=settings.DRIVE_BUCKET_NAME, Key=file.s3_key)
    except ClientError:
        pass
    file.delete()
    return HttpResponse("")


@cognito_login_required
def recycle_bin(request):
    """Show all soft-deleted files and folders within the 30-day window."""
    owner_sub = _get_owner_sub(request)
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=30)

    # Auto-permanently-delete expired files
    expired_files = DriveFile.objects.filter(owner_sub=owner_sub, deleted_at__lt=cutoff)
    s3 = _s3()
    for f in expired_files:
        try:
            s3.delete_object(Bucket=settings.DRIVE_BUCKET_NAME, Key=f.s3_key)
        except ClientError:
            pass
    expired_files.delete()

    # Auto-permanently-delete expired folders (and all files inside their subtree)
    expired_folders = DriveFolder.objects.filter(owner_sub=owner_sub, deleted_at__isnull=False, deleted_at__lt=cutoff)
    for folder in expired_folders:
        folder_ids = _collect_folder_ids(folder)
        files_inside = DriveFile.objects.filter(folder_id__in=folder_ids, owner_sub=owner_sub)
        for f in files_inside:
            try:
                s3.delete_object(Bucket=settings.DRIVE_BUCKET_NAME, Key=f.s3_key)
            except ClientError:
                pass
        files_inside.delete()
        folder.delete()  # CASCADE removes subfolders

    q = request.GET.get("q", "").strip()
    bin_files = DriveFile.objects.filter(owner_sub=owner_sub, deleted_at__isnull=False)
    bin_folders = DriveFolder.objects.filter(owner_sub=owner_sub, deleted_at__isnull=False)
    if q:
        bin_files = bin_files.filter(name__icontains=q)
        bin_folders = bin_folders.filter(name__icontains=q)

    # Annotate each deleted folder with its subtree content counts
    bin_folders_list = list(bin_folders)
    for folder in bin_folders_list:
        folder_ids = _collect_folder_ids(folder)
        folder.file_count = DriveFile.objects.filter(
            folder_id__in=folder_ids, deleted_at__isnull=True
        ).count()
        folder.subfolder_count = len(folder_ids) - 1

    ctx = {
        "files": bin_files,
        "bin_folders": bin_folders_list,
        "subfolders": [],
        "current_folder": None,
        "breadcrumbs": [],
        "is_recycle_bin": True,
        "search_query": q,
    }

    if request.headers.get("HX-Request"):
        return render(request, "drive/partials/search_results.html", ctx)

    from django.db.models import Prefetch
    active_subfolders = DriveFolder.objects.filter(deleted_at__isnull=True)
    ctx["sidebar_folders"] = DriveFolder.objects.filter(
        owner_sub=owner_sub, parent=None, deleted_at__isnull=True
    ).prefetch_related(
        Prefetch('subfolders', queryset=active_subfolders),
        Prefetch('subfolders__subfolders', queryset=active_subfolders),
        Prefetch('subfolders__subfolders__subfolders', queryset=active_subfolders),
    )
    _, ctx["storage_used"], ctx["storage_pct"] = _storage_stats(owner_sub)
    ctx["total_files"] = DriveFile.objects.filter(owner_sub=owner_sub, deleted_at__isnull=True).count()

    response = render(request, "drive/home.html", ctx)
    response["Cache-Control"] = "no-store"
    return response


@cognito_login_required
@require_POST
def archive_files(request):
    """Move selected files to Glacier Deep Archive and notify the user by email."""
    try:
        data      = json.loads(request.body)
        file_ids  = data.get("ids", [])
        owner_sub = _get_owner_sub(request)

        files        = DriveFile.objects.filter(id__in=file_ids, owner_sub=owner_sub)
        updated_html = []
        archived_names = []

        for f in files:
            _s3().copy_object(
                Bucket=settings.DRIVE_BUCKET_NAME,
                CopySource={"Bucket": settings.DRIVE_BUCKET_NAME, "Key": f.s3_key},
                Key=f.s3_key,
                StorageClass="DEEP_ARCHIVE",
                MetadataDirective="COPY",
            )
            f.storage_class = "DEEP_ARCHIVE"
            f.save(update_fields=["storage_class"])
            html = render(request, "drive/partials/file_row.html", {"file": f}).content.decode()
            updated_html.append({"id": f.id, "html": html})
            archived_names.append(f.name)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)

    # Email send is outside the try/except so S3 errors and email errors are independent
    user_email = request.session.get("user_email", "")
    if user_email and archived_names:
        try:
            _send_archive_email(user_email, archived_names)
        except Exception as email_err:
            logger.error("archive email failed: %s", email_err, exc_info=True)

    return JsonResponse({"updated": updated_html})


@cognito_login_required
def archive_view(request):
    """Show all archived files (Deep Archive / Glacier) across all folders."""
    owner_sub = _get_owner_sub(request)
    q = request.GET.get("q", "").strip()

    archived_files = DriveFile.objects.filter(
        owner_sub=owner_sub,
        storage_class=DriveFile.DEEP_ARCHIVE,
        deleted_at__isnull=True,
    ).exclude(restore_status=DriveFile.RESTORE_READY)
    if q:
        archived_files = archived_files.filter(name__icontains=q)

    ctx = {
        "files": archived_files,
        "subfolders": [],
        "current_folder": None,
        "breadcrumbs": [],
        "is_archive_view": True,
        "search_query": q,
    }

    if request.headers.get("HX-Request"):
        return render(request, "drive/partials/search_results.html", ctx)

    from django.db.models import Prefetch as _Prefetch
    _active_subfolders = DriveFolder.objects.filter(deleted_at__isnull=True)
    ctx["sidebar_folders"] = DriveFolder.objects.filter(
        owner_sub=owner_sub, parent=None, deleted_at__isnull=True
    ).prefetch_related(
        _Prefetch('subfolders', queryset=_active_subfolders),
        _Prefetch('subfolders__subfolders', queryset=_active_subfolders),
        _Prefetch('subfolders__subfolders__subfolders', queryset=_active_subfolders),
    )
    _, ctx["storage_used"], ctx["storage_pct"] = _storage_stats(owner_sub)
    ctx["total_files"] = DriveFile.objects.filter(owner_sub=owner_sub, deleted_at__isnull=True).count()

    response = render(request, "drive/home.html", ctx)
    response["Cache-Control"] = "no-store"
    return response


@cognito_login_required
@require_POST
def bulk_restore(request):
    """Initiate Glacier restore for multiple selected archived files."""
    data      = json.loads(request.body)
    file_ids  = data.get("ids", [])
    owner_sub = _get_owner_sub(request)
    user_email = request.session.get("user_email", "")

    files = DriveFile.objects.filter(
        id__in=file_ids,
        owner_sub=owner_sub,
        storage_class=DriveFile.DEEP_ARCHIVE,
        restore_status="",
        deleted_at__isnull=True,
    )

    updated_html = []
    restored_names = []

    for f in files:
        try:
            _s3().restore_object(
                Bucket=settings.DRIVE_BUCKET_NAME,
                Key=f.s3_key,
                RestoreRequest={"Days": 7, "GlacierJobParameters": {"Tier": "Standard"}},
            )
        except ClientError as e:
            code = e.response["Error"]["Code"]
            logger.error("bulk restore_object failed file=%s code=%s", f.pk, code)
            if code != "RestoreAlreadyInProgress":
                continue

        f.restore_status       = DriveFile.RESTORE_PENDING
        f.restore_notify_email = user_email
        f.save(update_fields=["restore_status", "restore_notify_email"])
        html = render(request, "drive/partials/file_row.html", {"file": f}).content.decode()
        updated_html.append({"id": f.id, "html": html})
        restored_names.append(f.name)

    if user_email and restored_names:
        try:
            _send_bulk_restore_email(user_email, restored_names)
        except Exception as email_err:
            logger.error("bulk restore email failed: %s", email_err, exc_info=True)

    return JsonResponse({"updated": updated_html})


def _send_bulk_restore_email(to_email, file_names):
    resend.api_key = _get_resend_api_key()
    count = len(file_names)
    noun  = "file" if count == 1 else "files"
    file_list_html = "".join(
        f'<li style="padding:4px 0;color:#cbd5e1;">{name}</li>'
        for name in file_names
    )
    html_body = f"""
    <div style="font-family:sans-serif;max-width:560px;margin:0 auto;background:#0f172a;padding:32px;border-radius:12px;">
        <h2 style="color:#f1f5f9;margin-top:0;">NovaDrive — Restore Started</h2>
        <p style="color:#94a3b8;">
            {count} {noun} are being restored from Glacier Deep Archive:
        </p>
        <ul style="background:#1e293b;border-radius:8px;padding:16px 16px 16px 32px;margin:16px 0;">
            {file_list_html}
        </ul>
        <p style="color:#94a3b8;">
            Retrieval typically takes <strong style="color:#f1f5f9;">12–48 hours</strong>.
            We'll send you another email as soon as your {noun} {"is" if count == 1 else "are"} ready to download.
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


@cognito_login_required
@require_POST
def restore_file(request, pk):
    """Initiate a Glacier restore and notify the user when it's ready."""
    file = get_object_or_404(DriveFile, pk=pk, owner_sub=_get_owner_sub(request))

    if not file.is_archived():
        return JsonResponse({"error": "File is not archived"}, status=400)

    if file.restore_status == DriveFile.RESTORE_PENDING:
        return JsonResponse({"error": "Restore already in progress"}, status=400)

    user_email = request.session.get("user_email", "")

    try:
        _s3().restore_object(
            Bucket=settings.DRIVE_BUCKET_NAME,
            Key=file.s3_key,
            RestoreRequest={
                "Days": 7,
                "GlacierJobParameters": {"Tier": "Standard"},
            },
        )
    except ClientError as e:
        code = e.response["Error"]["Code"]
        logger.error("restore_object failed code=%s err=%s", code, e)
        if code != "RestoreAlreadyInProgress":
            return JsonResponse({"error": str(e)}, status=400)

    file.restore_status       = DriveFile.RESTORE_PENDING
    file.restore_notify_email = user_email
    file.save(update_fields=["restore_status", "restore_notify_email"])

    if user_email:
        try:
            _send_restore_started_email(user_email, file.name)
        except Exception as email_err:
            logger.error("restore email failed: %s", email_err, exc_info=True)

    html = render(request, "drive/partials/file_row.html", {"file": file}).content.decode()
    return HttpResponse(html, content_type="text/html")


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
            We'll send you another email as soon as your file is ready to download.
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
    """Send a Resend email confirming files were moved to Glacier Deep Archive."""
    resend.api_key = _get_resend_api_key()

    file_list_html = "".join(
        f'<li style="padding:4px 0;color:#cbd5e1;">{name}</li>'
        for name in file_names
    )
    count = len(file_names)
    noun  = "file" if count == 1 else "files"

    html_body = f"""
    <div style="font-family:sans-serif;max-width:560px;margin:0 auto;background:#0f172a;padding:32px;border-radius:12px;">
        <h2 style="color:#f1f5f9;margin-top:0;">NovaDrive — Archive Confirmation</h2>
        <p style="color:#94a3b8;">
            {count} {noun} have been moved to <strong style="color:#a78bfa;">Glacier Deep Archive</strong>.
        </p>
        <ul style="background:#1e293b;border-radius:8px;padding:16px 16px 16px 32px;margin:16px 0;">
            {file_list_html}
        </ul>
        <p style="color:#64748b;font-size:13px;">
            Archived files cannot be previewed or downloaded directly. To restore them,
            you will need to initiate a Glacier restore request (retrieval time: 12–48 hours).
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
# Batch zip-folder
# ---------------------------------------------------------------------------

INLINE_ZIP_THRESHOLD = 0  # Always use Batch — Lambda timeout too short for inline zipping


def _collect_folder_files(folder_pk, owner_sub):
    """
    Iteratively walk the folder tree and return a list of (DriveFile, arc_path) tuples.
    Only non-archived, non-deleted files are included (archived files can't be read from S3).
    arc_path is the relative path inside the zip (e.g. "subfolder/file.jpg").
    """
    accessible = (DriveFile.GLACIER_IR,)
    result = []
    queue = [(folder_pk, "")]  # (folder_pk, path_prefix)
    visited = set()

    while queue:
        fk, prefix = queue.pop(0)
        if fk in visited:
            continue
        visited.add(fk)

        for sf in DriveFolder.objects.filter(parent_id=fk, owner_sub=owner_sub, deleted_at__isnull=True):
            child_prefix = f"{prefix}/{sf.name}" if prefix else sf.name
            queue.append((sf.pk, child_prefix))

        for f in DriveFile.objects.filter(
            folder_id=fk, owner_sub=owner_sub,
            deleted_at__isnull=True, storage_class__in=accessible,
        ):
            arc_path = f"{prefix}/{f.name}" if prefix else f.name
            result.append((f, arc_path))

    return result


def _folder_total_size(folder_pk, owner_sub):
    """Sum of accessible file sizes in the entire folder tree."""
    accessible = (DriveFile.GLACIER_IR,)
    queue = [folder_pk]
    visited = set()
    folder_pks = []

    while queue:
        fk = queue.pop()
        if fk in visited:
            continue
        visited.add(fk)
        folder_pks.append(fk)
        queue.extend(
            DriveFolder.objects.filter(parent_id=fk, owner_sub=owner_sub, deleted_at__isnull=True)
                        .values_list("pk", flat=True)
        )

    total = DriveFile.objects.filter(
        folder_id__in=folder_pks, owner_sub=owner_sub,
        deleted_at__isnull=True, storage_class__in=accessible,
    ).aggregate(total=Sum("size"))["total"] or 0
    return total


def _zip_and_upload(folder_pk, owner_sub, s3_client, bucket):
    """Zip all accessible files in folder tree, upload to temp-zips/, return S3 key."""
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for drv_file, arc_path in _collect_folder_files(folder_pk, owner_sub):
            try:
                obj = s3_client.get_object(Bucket=bucket, Key=drv_file.s3_key)
                zf.writestr(arc_path, obj["Body"].read())
            except Exception as e:
                logger.warning("zip_skip key=%s err=%s", drv_file.s3_key, e)
    buf.seek(0)
    zip_key = f"temp-zips/{uuid.uuid4()}.zip"
    s3_client.put_object(Bucket=bucket, Key=zip_key, Body=buf.getvalue(),
                         ContentType="application/zip")
    return zip_key


@cognito_login_required
@require_POST
def zip_folder(request, pk=None):
    """
    Zip one or more folders for download.
    Each folder gets its own independent Batch job so they run in parallel.
    Accepts JSON body: { "folder_ids": [1, 2, 3] } or falls back to URL pk.
    Returns: { "jobs": [{ "job_id": N, "folder_name": "..." }, ...] }
    """
    owner_sub = _get_owner_sub(request)
    bucket = settings.DRIVE_BUCKET_NAME

    try:
        body = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        body = {}

    folder_ids = body.get("folder_ids") or ([pk] if pk else [])
    if not folder_ids:
        return JsonResponse({"error": "No folders specified"}, status=400)

    folders = list(DriveFolder.objects.filter(pk__in=folder_ids, owner_sub=owner_sub, deleted_at__isnull=True))
    if len(folders) != len(folder_ids):
        return JsonResponse({"error": "One or more folders not found"}, status=404)

    batch = boto3.client("batch", region_name=settings.AWS_REGION)
    submitted = []

    for folder in folders:
        batch_job = BatchJob.objects.create(
            type="zip_folder",
            owner_sub=owner_sub,
            folder_name=folder.name,
            status=BatchJob.PENDING,
        )
        try:
            response = batch.submit_job(
                jobName=f"zip-folder-{folder.pk}-{batch_job.pk}",
                jobQueue=settings.BATCH_JOB_QUEUE,
                jobDefinition=settings.BATCH_JOB_DEFINITION,
                containerOverrides={
                    "environment": [
                        {"name": "JOB_TYPE",              "value": "zip_folder"},
                        {"name": "FOLDER_IDS",            "value": str(folder.pk)},
                        {"name": "OWNER_SUB",             "value": owner_sub},
                        {"name": "JOB_DB_ID",             "value": str(batch_job.pk)},
                        {"name": "DRIVE_BUCKET_NAME",     "value": bucket},
                        {"name": "AWS_REGION",            "value": settings.AWS_REGION},
                        {"name": "SSM_DATABASE_URL_NAME", "value": os.environ.get("SSM_DATABASE_URL_NAME", "")},
                    ]
                },
            )
            batch_job.job_id = response["jobId"]
            batch_job.save(update_fields=["job_id"])
            submitted.append({"job_id": batch_job.pk, "folder_name": folder.name})
        except Exception as e:
            logger.error("batch submit failed folder=%s: %s", folder.pk, e)
            batch_job.status = BatchJob.FAILED
            batch_job.save(update_fields=["status"])
            submitted.append({"job_id": batch_job.pk, "folder_name": folder.name, "error": str(e)})

    return JsonResponse({"status": "pending", "jobs": submitted})


@cognito_login_required
def job_status(request, job_id):
    """Poll the status of a batch job. Returns presigned URL when ready."""
    owner_sub = _get_owner_sub(request)
    job = get_object_or_404(BatchJob, pk=job_id, owner_sub=owner_sub)

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


@cognito_login_required
def timeline_view(request):
    """Google Photos-style timeline: all media grouped by capture/upload date, newest first."""
    import itertools
    from django.db.models import Q, Prefetch
    from django.db.models.functions import Coalesce

    owner_sub = _get_owner_sub(request)

    media = list(
        DriveFile.objects.filter(
            owner_sub=owner_sub,
            deleted_at__isnull=True,
        ).filter(
            Q(content_type__startswith="image/") | Q(content_type__startswith="video/")
        ).filter(
            Q(storage_class=DriveFile.GLACIER_IR)
            | Q(storage_class=DriveFile.DEEP_ARCHIVE, restore_status=DriveFile.RESTORE_READY)
        ).annotate(
            effective_date=Coalesce("captured_at", "uploaded_at")
        ).order_by("-effective_date")
    )

    # Group by calendar date (effective_date already set per file)
    def _date_key(f):
        return f.effective_date.date() if f.effective_date else datetime.date.min

    groups = [
        {"date": date, "files": list(files)}
        for date, files in itertools.groupby(media, key=_date_key)
    ]

    active_subfolders = DriveFolder.objects.filter(deleted_at__isnull=True)
    sidebar_folders = DriveFolder.objects.filter(
        owner_sub=owner_sub, parent=None, deleted_at__isnull=True
    ).prefetch_related(
        Prefetch('subfolders', queryset=active_subfolders),
        Prefetch('subfolders__subfolders', queryset=active_subfolders),
        Prefetch('subfolders__subfolders__subfolders', queryset=active_subfolders),
    )
    _, storage_used, storage_pct = _storage_stats(owner_sub)
    total_files = DriveFile.objects.filter(owner_sub=owner_sub, deleted_at__isnull=True).count()

    ctx = {
        "groups": groups,
        "total_media": len(media),
        "is_timeline_view": True,
        "sidebar_folders": sidebar_folders,
        "storage_used": storage_used,
        "storage_pct": storage_pct,
        "total_files": total_files,
        "files": DriveFile.objects.filter(owner_sub=owner_sub, deleted_at__isnull=True),
        "subfolders": DriveFolder.objects.none(),
        "current_folder": None,
        "breadcrumbs": [],
        "search_query": "",
    }

    response = render(request, "drive/home.html", ctx)
    response["Cache-Control"] = "no-store"
    return response
