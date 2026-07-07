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

# Thumbnail generator Lambda (async backfill invokes)
THUMBNAILER_FUNCTION = os.environ.get("THUMBNAILER_FUNCTION", "")

# AWS Batch (folder zip downloads)
BATCH_JOB_QUEUE      = os.environ.get("BATCH_JOB_QUEUE", "")
BATCH_JOB_DEFINITION = os.environ.get("BATCH_JOB_DEFINITION", "")

# Resend email
DRIVE_FROM_EMAIL = os.environ.get("DRIVE_FROM_EMAIL", "drive@nodepulsecaringal.xyz")

SSM_RESEND_API_KEY_NAME = os.environ.get("SSM_RESEND_API_KEY_NAME", "")
