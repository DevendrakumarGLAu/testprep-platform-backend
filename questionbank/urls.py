from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("subjects", views.SubjectViewSet, basename="subject")
router.register("topics", views.TopicViewSet, basename="topic")
router.register("tags", views.TagViewSet, basename="tag")
router.register("questions", views.QuestionViewSet, basename="question")
router.register("question-reports", views.QuestionReportViewSet, basename="question-report")

urlpatterns = router.urls
