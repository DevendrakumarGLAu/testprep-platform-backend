from django.urls import path

from . import admin_views as v

urlpatterns = [
    path("exams/<int:pk>/attempts/", v.ExamAttempts.as_view()),
    path("exams/<int:pk>/results-summary/", v.ExamResultsSummary.as_view()),
    path("attempts/<int:pk>/", v.AdminAttemptDetail.as_view()),
    *[
        path(f"exams/<int:pk>/{name}/", v.ReleaseControl.as_view(action_name=name))
        for name in v.CONTROLS
    ],
]
