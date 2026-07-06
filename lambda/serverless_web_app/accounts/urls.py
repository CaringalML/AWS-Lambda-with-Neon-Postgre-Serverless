from django.urls import path
from django.views.generic import RedirectView
from . import views

urlpatterns = [
    path("",                              RedirectView.as_view(pattern_name="signin", permanent=False)),
    path("signin/",                       views.signin,           name="signin"),
    path("signout/",                      views.signout,          name="signout"),
    path("dashboard/",                    views.dashboard,        name="dashboard"),
    path("forgot-password/",             views.forgot_password,  name="forgot_password"),
    path("reset-password/",              views.reset_password,   name="reset_password"),
]
