import hmac
from datetime import timedelta

import boto3
import resend
from django.conf import settings
from django.shortcuts import render, redirect
from django.urls import reverse
from django.utils import timezone

from .decorators import cognito_login_required
from .forms import SignInForm
from .models import PasswordResetToken


def _get_admin_password():
    ssm = boto3.client("ssm", region_name=settings.AWS_REGION)
    return ssm.get_parameter(
        Name=settings.SSM_ADMIN_PASSWORD_NAME, WithDecryption=True
    )["Parameter"]["Value"]


def _get_resend_api_key():
    ssm = boto3.client("ssm", region_name=settings.AWS_REGION)
    return ssm.get_parameter(
        Name=settings.SSM_RESEND_API_KEY_NAME, WithDecryption=True
    )["Parameter"]["Value"]


def signin(request):
    if request.session.get("access_token"):
        return redirect("drive_home")
    form = SignInForm(request.POST or None)
    if form.is_valid():
        email    = form.cleaned_data["email"]
        password = form.cleaned_data["password"]
        email_ok = hmac.compare_digest(email.lower(), settings.ADMIN_EMAIL.lower())
        pass_ok  = hmac.compare_digest(password, _get_admin_password())
        if email_ok and pass_ok:
            request.session["access_token"] = "admin"
            request.session["user_sub"]     = "admin"
            request.session["user_email"]   = settings.ADMIN_EMAIL
            return redirect("drive_home")
        form.add_error(None, "Invalid email or password.")
    return render(request, "accounts/signin.html", {"form": form})


@cognito_login_required
def dashboard(request):
    user = {
        "email":          request.session.get("user_email", ""),
        "email_verified": True,
        "sub":            "admin",
        "username":       request.session.get("user_email", ""),
    }
    return render(request, "accounts/dashboard.html", {"user": user})


def signout(request):
    request.session.flush()
    return redirect("signin")


def forgot_password(request):
    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        if hmac.compare_digest(email, settings.ADMIN_EMAIL.lower()):
            token = PasswordResetToken.objects.create(
                expires_at=timezone.now() + timedelta(minutes=15)
            )
            reset_url = request.build_absolute_uri(
                reverse("reset_password", kwargs={"token": str(token.token)})
            )
            try:
                _send_reset_email(settings.ADMIN_EMAIL, reset_url)
            except Exception:
                pass
        return render(request, "accounts/forgot_password.html", {"sent": True})
    return render(request, "accounts/forgot_password.html", {"sent": False})


def reset_password(request, token):
    try:
        token_obj = PasswordResetToken.objects.get(token=token)
    except PasswordResetToken.DoesNotExist:
        return render(request, "accounts/reset_password.html", {"invalid": True})

    if not token_obj.is_valid():
        return render(request, "accounts/reset_password.html", {"invalid": True})

    error = None
    if request.method == "POST":
        new_password = request.POST.get("password", "")
        confirm      = request.POST.get("confirm_password", "")
        if len(new_password) < 8:
            error = "Password must be at least 8 characters."
        elif new_password != confirm:
            error = "Passwords do not match."
        else:
            ssm = boto3.client("ssm", region_name=settings.AWS_REGION)
            ssm.put_parameter(
                Name=settings.SSM_ADMIN_PASSWORD_NAME,
                Value=new_password,
                Overwrite=True,
                Type="SecureString",
            )
            token_obj.used = True
            token_obj.save()
            return render(request, "accounts/reset_password.html", {"success": True})

    return render(request, "accounts/reset_password.html", {"token": token, "error": error})


def _send_reset_email(to_email, reset_url):
    html_body = f"""
    <div style="font-family:Inter,sans-serif;max-width:520px;margin:0 auto;background:#0d0f14;border-radius:12px;overflow:hidden;border:1px solid #1e293b;">
        <div style="background:linear-gradient(135deg,#0c1e4a,#0f0a3a);padding:32px;text-align:center;">
            <h1 style="color:#fff;font-size:22px;margin:0;">NovaDrive</h1>
        </div>
        <div style="padding:32px;">
            <h2 style="color:#f1f5f9;font-size:18px;margin:0 0 12px;">Reset your password</h2>
            <p style="color:#94a3b8;font-size:14px;line-height:1.6;margin:0 0 24px;">
                Click the button below to set a new password. This link expires in <strong style="color:#f1f5f9;">15 minutes</strong>.
            </p>
            <a href="{reset_url}"
               style="display:inline-block;background:#0ea5e9;color:#fff;font-weight:600;font-size:14px;padding:12px 28px;border-radius:8px;text-decoration:none;">
                Reset password
            </a>
            <p style="color:#475569;font-size:12px;margin:24px 0 0;">
                If you didn't request this, you can safely ignore this email.
            </p>
        </div>
        <div style="padding:16px 32px;border-top:1px solid #1e293b;text-align:center;">
            <p style="color:#475569;font-size:12px;margin:0;">NovaDrive &nbsp;·&nbsp; nodepulsecaringal.xyz</p>
        </div>
    </div>
    """
    resend.api_key = _get_resend_api_key()
    resend.Emails.send({
        "from":    settings.DRIVE_FROM_EMAIL,
        "to":      [to_email],
        "subject": "NovaDrive: Reset your password",
        "html":    html_body,
    })
