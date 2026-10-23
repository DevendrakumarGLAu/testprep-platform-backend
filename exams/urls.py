from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views
from .public import PublicExamPage, PublicHome

router = DefaultRouter()
router.register("batches", views.BatchViewSet, basename="batch")
router.register("exam-categories", views.ExamCategoryViewSet, basename="exam-category")
router.register("exams", views.ExamViewSet, basename="exam")

urlpatterns = [path("public/home/", PublicHome.as_view()),
    path("public/exams/<str:pid>/", PublicExamPage.as_view()), *router.urls]
