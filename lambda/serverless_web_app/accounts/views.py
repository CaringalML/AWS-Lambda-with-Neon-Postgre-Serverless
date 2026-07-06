import hmac

import boto3
from botocore.exceptions import ClientError
from django.conf import settings
from django.shortcuts import render, redirect

from .decorators import cognito_login_required
from .forms import SignInForm


def _get_admin_password():
    ssm = boto3.client("ssm", region_name=settings.AWS_REGION)
    return ssm.get_parameter(
        Name=settings.SSM_ADMIN_PASSWORD_NAME, WithDecryption=True
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
            try:
                cognito = boto3.client("cognito-idp", region_name=settings.AWS_REGION)
                cognito.forgot_password(
                    ClientId=settings.COGNITO_CLIENT_ID,
                    Username=settings.ADMIN_EMAIL,
                )
            except Exception:
                pass
        return render(request, "accounts/forgot_password.html", {"sent": True})
    return render(request, "accounts/forgot_password.html", {"sent": False})


def reset_password(request):
    error = None
    if request.method == "POST":
        code         = request.POST.get("code", "").strip()
        new_password = request.POST.get("password", "")
        confirm      = request.POST.get("confirm_password", "")
        if not code:
            error = "Please enter the verification code from your email."
        elif len(new_password) < 8:
            error = "Password must be at least 8 characters."
        elif new_password != confirm:
            error = "Passwords do not match."
        else:
            try:
                cognito = boto3.client("cognito-idp", region_name=settings.AWS_REGION)
                cognito.confirm_forgot_password(
                    ClientId=settings.COGNITO_CLIENT_ID,
                    Username=settings.ADMIN_EMAIL,
                    ConfirmationCode=code,
                    Password=new_password,
                )
                ssm = boto3.client("ssm", region_name=settings.AWS_REGION)
                ssm.put_parameter(
                    Name=settings.SSM_ADMIN_PASSWORD_NAME,
                    Value=new_password,
                    Overwrite=True,
                    Type="SecureString",
                )
                return render(request, "accounts/reset_password.html", {"success": True})
            except ClientError as e:
                err_code = e.response["Error"]["Code"]
                if err_code == "CodeMismatchException":
                    error = "Invalid verification code. Please check your email and try again."
                elif err_code == "ExpiredCodeException":
                    error = "Verification code has expired. Please request a new one."
                elif err_code == "InvalidPasswordException":
                    error = "Password doesn't meet requirements: min 8 characters, uppercase, lowercase, and number."
                else:
                    error = "Something went wrong. Please try again."
    return render(request, "accounts/reset_password.html", {"error": error})
