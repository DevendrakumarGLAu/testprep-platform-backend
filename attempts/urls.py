from django.urls import path

from . import views

urlpatterns = [
    path("dashboard/", views.StudentDashboard.as_view()),
    path("filters/", views.StudentFilters.as_view()),
    path("prep/", views.PrepList.as_view()),
    path("prep/<str:pid>/", views.PrepHub.as_view()),
    path("exams/", views.StudentExamList.as_view()),
    path("exams/<str:pid>/", views.StudentExamDetail.as_view()),
    path("exams/<str:pid>/start/", views.StartAttempt.as_view()),
    path("attempts/", views.AttemptHistory.as_view()),
    path("attempts/<str:pid>/", views.AttemptDetail.as_view()),
    path("attempts/<str:pid>/answers/", views.SaveAnswers.as_view()),
    path("attempts/<str:pid>/result/", views.AttemptResult.as_view()),
    path("attempts/<str:pid>/submit/", views.SubmitAttempt.as_view()),
    path("attempts/<str:pid>/sections/submit/", views.SubmitSection.as_view()),
]
