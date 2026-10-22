from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("imports", views.ImportViewSet, basename="import")

urlpatterns = [
    path("imports/sources/", views.SourceListView.as_view()),
    path("imports/template/", views.TemplateView.as_view()),
    *router.urls,
]
