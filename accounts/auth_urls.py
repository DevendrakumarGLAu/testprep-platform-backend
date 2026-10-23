from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from . import views

urlpatterns = [
    path("captcha/", views.CaptchaView.as_view()),
    path("login/", views.LoginView.as_view()),
    path("forgot-password/", views.ForgotPasswordView.as_view()),
    path("reset-password/", views.ResetPasswordView.as_view()),
    path("send-verification/", views.SendVerificationView.as_view()),
    path("verify-email/", views.VerifyEmailView.as_view()),
    path("refresh/", TokenRefreshView.as_view()),
    path("register/", views.RegisterView.as_view()),
    path("me/", views.MeView.as_view()),
    path("change-password/", views.ChangePasswordView.as_view()),
    path("logout/", views.LogoutView.as_view()),
]
