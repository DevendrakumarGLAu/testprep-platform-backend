from django.urls import path

from . import student_views

urlpatterns = [
    path("notes/", student_views.NoteList.as_view()),
    path("notes/<str:pid>/", student_views.NoteDetail.as_view()),
    path("series/", student_views.SeriesList.as_view()),
    path("series/<str:pid>/", student_views.SeriesDetail.as_view()),
]
