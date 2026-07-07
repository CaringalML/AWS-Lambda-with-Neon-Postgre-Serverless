from __future__ import annotations
import datetime
from dataclasses import dataclass, field


def _parse_dt(s) -> datetime.datetime | None:
    if not s:
        return None
    if isinstance(s, datetime.datetime):
        return s
    return datetime.datetime.fromisoformat(s)


class _ListProxy:
    """Wraps a list so templates can iterate directly or call .all()."""
    def __init__(self, items=None):
        self._items = list(items) if items else []

    def all(self):
        return self._items

    def __iter__(self):
        return iter(self._items)

    def __len__(self):
        return len(self._items)

    def __bool__(self):
        return bool(self._items)

    @property
    def count(self):
        return len(self._items)


@dataclass
class DriveFolder:
    GLACIER_IR   = "GLACIER_IR"
    DEEP_ARCHIVE = "DEEP_ARCHIVE"

    folder_id:  str
    owner_sub:  str
    name:       str
    parent_id:  str | None
    created_at: str
    deleted_at: str | None = None

    # Populated by DAL when building the sidebar tree
    subfolders: _ListProxy = field(default_factory=_ListProxy, compare=False, repr=False)

    @property
    def pk(self):
        return self.folder_id

    @property
    def id(self):
        return self.folder_id

    def is_deleted(self):
        return self.deleted_at is not None

    def days_until_permanent_delete(self):
        if not self.deleted_at:
            return None
        expires = _parse_dt(self.deleted_at) + datetime.timedelta(days=30)
        remaining = (expires - datetime.datetime.now(datetime.timezone.utc)).days
        return max(remaining, 0)

    def __str__(self):
        return self.name


@dataclass
class DriveFile:
    GLACIER_IR      = "GLACIER_IR"
    DEEP_ARCHIVE    = "DEEP_ARCHIVE"
    RESTORE_PENDING = "pending"
    RESTORE_READY   = "ready"

    STORAGE_CLASS_CHOICES = [
        ("GLACIER_IR",   "Glacier Instant Retrieval"),
        ("DEEP_ARCHIVE", "Deep Archive"),
    ]

    file_id:              str
    owner_sub:            str
    folder_id:            str | None
    name:                 str
    s3_key:               str
    size:                 int
    content_type:         str
    storage_class:        str
    uploaded_at:          str
    restore_status:       str       = ""
    restore_notify_email: str       = ""
    restore_expires_at:   str | None = None
    deleted_at:           str | None = None
    captured_at:          str | None = None

    @property
    def pk(self):
        return self.file_id

    @property
    def id(self):
        return self.file_id

    @property
    def effective_date(self):
        return _parse_dt(self.captured_at or self.uploaded_at)

    def get_storage_class_display(self):
        return dict(self.STORAGE_CLASS_CHOICES).get(self.storage_class, self.storage_class)

    def size_display(self):
        size = float(self.size)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    def is_viewable_inline(self):
        return self.content_type.startswith(
            ("image/", "video/", "audio/", "application/pdf", "text/")
        )

    def is_archived(self):
        return self.storage_class == self.DEEP_ARCHIVE

    def is_deleted(self):
        return self.deleted_at is not None

    def storage_class_label(self):
        if self.is_archived() and self.restore_status == self.RESTORE_READY:
            return "Restored from Deep Archive"
        return self.get_storage_class_display()

    def days_until_permanent_delete(self):
        if not self.deleted_at:
            return None
        expires = _parse_dt(self.deleted_at) + datetime.timedelta(days=30)
        remaining = (expires - datetime.datetime.now(datetime.timezone.utc)).days
        return max(remaining, 0)


@dataclass
class BatchJob:
    PENDING = "pending"
    RUNNING = "running"
    READY   = "ready"
    FAILED  = "failed"

    job_id:      str
    owner_sub:   str
    aws_job_id:  str
    type:        str
    folder_name: str
    status:      str
    result_key:  str       = ""
    progress:    int       = 0
    created_at:  str       = ""
    expires_at:  str | None = None

    @property
    def pk(self):
        return self.job_id

    @property
    def id(self):
        return self.job_id

    def __str__(self):
        return f"BatchJob({self.type}, {self.status}, folder={self.folder_name})"
