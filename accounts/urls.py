from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("users", views.UserViewSet, basename="user")
router.register("activity-logs", views.ActivityLogViewSet, basename="activity-log")

urlpatterns = [
    path("platform/", views.PlatformView.as_view()),
    *router.urls,
]
