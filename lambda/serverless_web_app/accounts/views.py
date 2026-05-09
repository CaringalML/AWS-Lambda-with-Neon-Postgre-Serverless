import hmac

from django.conf import settings
from django.shortcuts import render, redirect

from .decorators import cognito_login_required
from .forms import SignInForm


def signin(request):
    if request.session.get("access_token"):
        return redirect("drive_home")
    form = SignInForm(request.POST or None)
    if form.is_valid():
        email = form.cleaned_data["email"]
        password = form.cleaned_data["password"]
        email_ok = hmac.compare_digest(email.lower(), settings.ADMIN_EMAIL.lower())
        pass_ok  = hmac.compare_digest(password, settings.ADMIN_PASSWORD)
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
