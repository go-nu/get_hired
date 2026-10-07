from django.urls import path

from . import views

app_name = "applications"

urlpatterns = [
    path("new/", views.ApplicationCreateView.as_view(), name="create"),
    path("posting-ocr/", views.PostingOcrView.as_view(), name="posting_ocr"),
    path("memo-ocr/", views.MemoOcrView.as_view(), name="memo_ocr"),
    path(
        "no-response/fail/",
        views.NoResponseFailView.as_view(),
        name="no_response_fail",
    ),
    path("<int:pk>/", views.ApplicationDetailView.as_view(), name="detail"),
    path("<int:pk>/edit/", views.ApplicationUpdateView.as_view(), name="update"),
    path("<int:pk>/stage/", views.ApplicationStageUpdateView.as_view(), name="stage"),
    path(
        "<int:pk>/analysis/new/",
        views.AnalysisCreateView.as_view(),
        name="analysis_create",
    ),
    path(
        "<int:pk>/analysis/run/",
        views.AnalysisRunView.as_view(),
        name="analysis_run",
    ),
    path(
        "<int:pk>/analysis/resume/",
        views.AnalysisResumeView.as_view(),
        name="analysis_resume",
    ),
    path("companies/", views.CompanyListView.as_view(), name="company_list"),
    path(
        "companies/deleted/",
        views.DeletedCompanyListView.as_view(),
        name="company_deleted_list",
    ),
    path(
        "companies/<int:pk>/edit/",
        views.CompanyUpdateView.as_view(),
        name="company_update",
    ),
    path(
        "companies/<int:pk>/delete/",
        views.CompanyDeleteView.as_view(),
        name="company_delete",
    ),
    path(
        "companies/<int:pk>/restore/",
        views.CompanyRestoreView.as_view(),
        name="company_restore",
    ),
]
