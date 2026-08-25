from django.conf import settings


def cloudfront(request):
    mb = settings.MAX_UPLOAD_BYTES / 1024 / 1024
    return {
        "CLOUDFRONT_DOMAIN": settings.CLOUDFRONT_DOMAIN,
        # Shown next to the upload buttons so the cap is known before, not after
        "MAX_UPLOAD_DISPLAY": f"{mb / 1024:.0f} GB" if mb >= 1024 else f"{mb:.0f} MB",
    }
