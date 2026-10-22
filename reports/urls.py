from django.urls import path

from . import views

urlpatterns = [
    path("admin/dashboard/", views.AdminDashboard.as_view()),
    path("admin/search/", views.GlobalSearch.as_view()),
    path("students/", views.StudentList.as_view()),
    path("students/<int:pk>/", views.StudentDetail.as_view()),
    path("students/<int:pk>/assign-tests/", views.StudentAssignTests.as_view()),
    path("students/<int:pk>/batches/", views.StudentBatches.as_view()),
    path("students/<int:pk>/report.pdf", views.StudentReportPDF.as_view()),
    path("reports/consolidated.pdf", views.ConsolidatedReportPDF.as_view()),
]
