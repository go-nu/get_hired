from django.urls import path

from . import views

app_name = "agents"

urlpatterns = [
    path("settings/", views.AgentSettingsView.as_view(), name="settings"),
    path("runs/", views.AgentRunListView.as_view(), name="run_list"),
    path("runs/<int:pk>/", views.AgentRunDetailView.as_view(), name="run_detail"),
]
