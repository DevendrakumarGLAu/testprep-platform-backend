from django.urls import path

from . import leaderboard, practice_views, saved_views, views, weekly_views

urlpatterns = [
    path("dashboard/", views.StudentDashboard.as_view()),
    path("filters/", views.StudentFilters.as_view()),
    path("prep/", views.PrepList.as_view()),
    path("prep/<str:pid>/", views.PrepHub.as_view()),
    path("weekly-summary/", weekly_views.WeeklySummary.as_view()),
    path("revise/", weekly_views.Revise.as_view()),
    path("weak-topics/", practice_views.WeakTopics.as_view()),
    path("practice/weak/", practice_views.PracticeWeak.as_view()),
    path("daily-quiz/", practice_views.DailyQuiz.as_view()),
    path("attempts/<str:pid>/practice-wrong/", practice_views.PracticeWrong.as_view()),
    path("saved/", saved_views.SavedList.as_view()),
    path("saved/<int:pk>/", saved_views.SavedDetail.as_view()),
    path("reports/", saved_views.MyReports.as_view()),
    path("attempts/<str:pid>/questions/<int:number>/save/", saved_views.SaveQuestion.as_view()),
    path("attempts/<str:pid>/questions/<int:number>/report/", saved_views.ReportQuestion.as_view()),
    path("exams/", views.StudentExamList.as_view()),
    path("exams/<str:pid>/", views.StudentExamDetail.as_view()),
    path("exams/<str:pid>/start/", views.StartAttempt.as_view()),
    path("exams/<str:pid>/leaderboard/", leaderboard.Leaderboard.as_view()),
    path("attempts/", views.AttemptHistory.as_view()),
    path("attempts/<str:pid>/", views.AttemptDetail.as_view()),
    path("attempts/<str:pid>/answers/", views.SaveAnswers.as_view()),
    path("attempts/<str:pid>/result/", views.AttemptResult.as_view()),
    path("attempts/<str:pid>/submit/", views.SubmitAttempt.as_view()),
    path("attempts/<str:pid>/sections/submit/", views.SubmitSection.as_view()),
]
