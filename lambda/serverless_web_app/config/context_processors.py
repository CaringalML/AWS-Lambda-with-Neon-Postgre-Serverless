from django.conf import settings


def cloudfront(request):
    return {"CLOUDFRONT_DOMAIN": settings.CLOUDFRONT_DOMAIN}
