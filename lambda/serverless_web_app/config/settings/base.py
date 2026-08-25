import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "change-me-in-production")

ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.staticfiles",
    "django.contrib.sessions",
    "accounts",
    "drive",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "config.context_processors.cloudfront",
            ],
        },
    },
]

DATABASES = {}

SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]

# AWS
AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")

# Single-admin auth
ADMIN_EMAIL             = os.environ.get("ADMIN_EMAIL", "")
SSM_ADMIN_PASSWORD_NAME = os.environ.get("SSM_ADMIN_PASSWORD_NAME", "")
COGNITO_CLIENT_ID       = os.environ.get("COGNITO_CLIENT_ID", "")

# NovaDrive
DRIVE_BUCKET_NAME               = os.environ.get("DRIVE_BUCKET_NAME", "")
CLOUDFRONT_DOMAIN               = os.environ.get("CLOUDFRONT_DOMAIN", "")
CLOUDFRONT_KEY_PAIR_ID          = os.environ.get("CLOUDFRONT_KEY_PAIR_ID", "")
CLOUDFRONT_PRIVATE_KEY_SSM_NAME = os.environ.get("CLOUDFRONT_PRIVATE_KEY_SSM_NAME", "")

# DynamoDB tables
DYNAMODB_FOLDERS_TABLE    = os.environ.get("DYNAMODB_FOLDERS_TABLE", "")
DYNAMODB_FILES_TABLE      = os.environ.get("DYNAMODB_FILES_TABLE", "")
DYNAMODB_BATCH_JOBS_TABLE = os.environ.get("DYNAMODB_BATCH_JOBS_TABLE", "")
DYNAMODB_UPLOAD_FAILURES_TABLE = os.environ.get("DYNAMODB_UPLOAD_FAILURES_TABLE", "")

# Largest upload we accept at all. Shown in the UI and checked in the browser
# so oversized files are rejected before any bytes go on the wire.
MAX_UPLOAD_BYTES = 100 * 1024 ** 3          # 100 GB

# Files at or above this go through multipart. Below it, a single presigned
# POST is fewer round trips and there's little to gain from chunking.
MULTIPART_THRESHOLD = 100 * 1024 ** 2       # 100 MB

# Part size. S3 allows at most 10,000 parts, so this also sets the ceiling
# of the multipart path (100 MB x 10,000 = ~1 TB) — well past MAX_UPLOAD_BYTES.
MULTIPART_PART_SIZE = 100 * 1024 ** 2       # 100 MB

# Part URLs must outlive the whole transfer: 100 GB on a slow line runs for
# hours, and a URL that expires mid-upload fails the part with a 403.
MULTIPART_URL_EXPIRY = 6 * 3600

# Thumbnail generator Lambda (async backfill invokes)
THUMBNAILER_FUNCTION = os.environ.get("THUMBNAILER_FUNCTION", "")

# AWS Batch (folder zip downloads)
BATCH_JOB_QUEUE      = os.environ.get("BATCH_JOB_QUEUE", "")
BATCH_JOB_DEFINITION = os.environ.get("BATCH_JOB_DEFINITION", "")

# Resend email
DRIVE_FROM_EMAIL = os.environ.get("DRIVE_FROM_EMAIL", "drive@nodepulsecaringal.xyz")

SSM_RESEND_API_KEY_NAME = os.environ.get("SSM_RESEND_API_KEY_NAME", "")
