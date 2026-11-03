from django.urls import path

from . import doubt_views, plan_views

urlpatterns = [
    path("study-plan/", plan_views.StudyPlanView.as_view()),
    path("study-plan/reschedule/", plan_views.StudyPlanReschedule.as_view()),
    path("study-plan/refresh/", plan_views.StudyPlanRefresh.as_view()),
    path("study-plan/tasks/<int:pk>/done/", plan_views.StudyTaskDone.as_view()),
    path("doubts/", doubt_views.MyDoubts.as_view()),
    path("doubts/<int:pk>/", doubt_views.MyDoubtDetail.as_view()),
    path("doubts/<int:pk>/reply/", doubt_views.MyDoubtReply.as_view()),
    path("doubts/<int:pk>/resolve/", doubt_views.MyDoubtResolve.as_view()),
    path("attempts/<str:pid>/questions/<int:number>/doubt/", doubt_views.AskDoubt.as_view()),
]
