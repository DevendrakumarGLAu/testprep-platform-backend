from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from . import views

urlpatterns = [
    path("login/", views.LoginView.as_view()),
    path("refresh/", TokenRefreshView.as_view()),
    path("register/", views.RegisterView.as_view()),
    path("me/", views.MeView.as_view()),
    path("change-password/", views.ChangePasswordView.as_view()),
    path("logout/", views.LogoutView.as_view()),
]
