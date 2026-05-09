from django.core.exceptions import ValidationError
from django.db import models


class DriveFolder(models.Model):
    owner_sub  = models.CharField(max_length=128, db_index=True)
    name       = models.CharField(max_length=255)
    parent     = models.ForeignKey(
        'self', null=True, blank=True,
        on_delete=models.CASCADE, related_name='subfolders',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ['name']
        unique_together = [('owner_sub', 'parent', 'name')]

    def clean(self):
        if self.pk and self.parent_id == self.pk:
            raise ValidationError("A folder cannot be its own parent.")

    def __str__(self):
        return self.name

    def days_until_permanent_delete(self):
        if not self.deleted_at:
            return None
        import datetime
        expires = self.deleted_at + datetime.timedelta(days=30)
        remaining = (expires - datetime.datetime.now(datetime.timezone.utc)).days
        return max(remaining, 0)


class DriveFile(models.Model):
    GLACIER_IR    = "GLACIER_IR"
    DEEP_ARCHIVE  = "DEEP_ARCHIVE"

    STORAGE_CLASS_CHOICES = [
        (GLACIER_IR,   "Glacier Instant Retrieval"),
        (DEEP_ARCHIVE, "Deep Archive"),
    ]

    # Cognito user sub — ties file to owner without a users table
    owner_sub    = models.CharField(max_length=128, db_index=True)
    folder       = models.ForeignKey(
        DriveFolder, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='files',
    )
    name         = models.CharField(max_length=255)
    s3_key       = models.CharField(max_length=512, unique=True)
    size         = models.BigIntegerField(default=0)
    content_type = models.CharField(max_length=100, default="application/octet-stream")
    storage_class = models.CharField(
        max_length=20,
        choices=STORAGE_CLASS_CHOICES,
        default=GLACIER_IR,
    )
    uploaded_at  = models.DateTimeField(auto_now_add=True)

    RESTORE_PENDING = "pending"
    RESTORE_READY   = "ready"
    RESTORE_STATUS_CHOICES = [
        (RESTORE_PENDING, "Restoring"),
        (RESTORE_READY,   "Ready"),
    ]
    restore_status       = models.CharField(max_length=10, blank=True, default="",
                                            choices=RESTORE_STATUS_CHOICES)
    restore_notify_email = models.EmailField(blank=True, default="")
    restore_expires_at   = models.DateTimeField(null=True, blank=True)

    deleted_at   = models.DateTimeField(null=True, blank=True, db_index=True)
    captured_at  = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def size_display(self):
        size = float(self.size)
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    def is_viewable_inline(self):
        return self.content_type.startswith(("image/", "video/", "audio/", "application/pdf", "text/"))

    def is_archived(self):
        return self.storage_class == self.DEEP_ARCHIVE

    def storage_class_label(self):
        if self.is_archived() and self.restore_status == self.RESTORE_READY:
            return "Restored from Deep Archive"
        return self.get_storage_class_display()

    def is_deleted(self):
        return self.deleted_at is not None

    def days_until_permanent_delete(self):
        if not self.deleted_at:
            return None
        import datetime
        expires = self.deleted_at + datetime.timedelta(days=30)
        remaining = (expires - datetime.datetime.now(datetime.timezone.utc)).days
        return max(remaining, 0)


class BatchJob(models.Model):
    PENDING = "pending"
    RUNNING = "running"
    READY   = "ready"
    FAILED  = "failed"

    STATUS_CHOICES = [
        (PENDING, "Pending"),
        (RUNNING, "Running"),
        (READY,   "Ready"),
        (FAILED,  "Failed"),
    ]

    job_id      = models.CharField(max_length=128, blank=True, default="")  # AWS Batch job ID
    type        = models.CharField(max_length=32, default="zip_folder")
    owner_sub   = models.CharField(max_length=128, db_index=True)
    folder_name = models.CharField(max_length=255, default="")
    status      = models.CharField(max_length=16, choices=STATUS_CHOICES, default=PENDING)
    result_key  = models.CharField(max_length=512, blank=True, default="")  # S3 key of zip
    progress    = models.IntegerField(default=0)                            # 0-100
    created_at  = models.DateTimeField(auto_now_add=True)
    expires_at  = models.DateTimeField(null=True, blank=True)  # 24h from ready

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"BatchJob({self.type}, {self.status}, folder={self.folder_name})"
