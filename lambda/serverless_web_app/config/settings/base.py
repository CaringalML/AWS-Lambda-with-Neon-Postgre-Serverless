import os
import dj_database_url
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "change-me-in-production")


def _get_database_url():
    """
    Fetch the database URL from SSM Parameter Store when running on Lambda.
    Falls back to DATABASE_URL env var for local development.
    """
    # Local dev: set DATABASE_URL directly in the environment
    if url := os.environ.get("DATABASE_URL"):
        return url

    # Lambda: fetch from SSM using the parameter name injected as an env var
    param_name = os.environ.get("SSM_DATABASE_URL_NAME")
    if param_name:
        import boto3
        ssm = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "ap-southeast-2"))
        response = ssm.get_parameter(Name=param_name, WithDecryption=True)
        return response["Parameter"]["Value"]

    return None

ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
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
            ],
        },
    },
]

DATABASES = {
    "default": dj_database_url.parse(
        _get_database_url(),
        conn_max_age=60,
        ssl_require=True,
    )
}

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

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

# AWS Batch (folder zip downloads)
BATCH_JOB_QUEUE      = os.environ.get("BATCH_JOB_QUEUE", "")
BATCH_JOB_DEFINITION = os.environ.get("BATCH_JOB_DEFINITION", "")

# Resend email
DRIVE_FROM_EMAIL = os.environ.get("DRIVE_FROM_EMAIL", "drive@nodepulsecaringal.xyz")

SSM_RESEND_API_KEY_NAME = os.environ.get("SSM_RESEND_API_KEY_NAME", "")
