from django.urls import path

from . import views

urlpatterns = [
    path("", views.workspace_list, name="list"),
    path("new/", views.create, name="create"),
    path("switch/<slug:slug>/", views.switch, name="switch"),
    path("settings/", views.settings_view, name="settings"),
    path("hours/", views.hours, name="hours"),
    path("hours/holiday/", views.holiday_add, name="holiday_add"),
    path("hours/holiday/<int:pk>/delete/", views.holiday_delete, name="holiday_delete"),
    path("team/", views.team, name="team"),
    path("team/<int:pk>/remove/", views.member_remove, name="member_remove"),
]
