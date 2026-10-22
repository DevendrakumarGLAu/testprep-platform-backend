from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("batches", views.BatchViewSet, basename="batch")
router.register("exam-categories", views.ExamCategoryViewSet, basename="exam-category")
router.register("exams", views.ExamViewSet, basename="exam")

urlpatterns = router.urls
