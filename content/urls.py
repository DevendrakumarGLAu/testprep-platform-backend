from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("study-notes", views.StudyNoteViewSet, basename="study-note")
router.register("test-series", views.TestSeriesViewSet, basename="test-series")

urlpatterns = router.urls
