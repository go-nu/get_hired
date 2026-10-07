from django.urls import path

from . import views

app_name = "profiles"

urlpatterns = [
    path("", views.ActiveVersionView.as_view(), name="active"),
    path("new/", views.VersionCreateView.as_view(), name="create"),
    path("versions/", views.VersionListView.as_view(), name="list"),
    path("versions/compare/", views.VersionCompareView.as_view(), name="compare"),
    path("versions/<int:version>/", views.VersionDetailView.as_view(), name="detail"),
    path(
        "versions/<int:version>/activate/",
        views.VersionActivateView.as_view(),
        name="activate",
    ),
]
