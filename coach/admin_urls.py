from django.urls import path

from . import doubt_views, feedback_views, progress_views

urlpatterns = [
    path("admin/doubts/", doubt_views.AdminDoubts.as_view()),
    path("admin/doubts/<int:pk>/", doubt_views.AdminDoubtDetail.as_view()),
    path("admin/doubts/<int:pk>/reply/", doubt_views.AdminDoubtReply.as_view()),
    path("admin/doubts/<int:pk>/status/", doubt_views.AdminDoubtStatus.as_view()),
    path("admin/doubts/<int:pk>/ai-suggest/", doubt_views.AdminDoubtAISuggest.as_view()),
    path("admin/batches/<int:pk>/progress/", progress_views.BatchProgress.as_view()),
    path("feedback/", feedback_views.FeedbackView.as_view()),
    path("feedback/<int:pk>/status/", feedback_views.FeedbackStatus.as_view()),
]
