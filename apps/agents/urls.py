from django.urls import path

from . import views

urlpatterns = [
    path("me/", views.me, name="me"),
    path("me/presence/<str:presence>/", views.set_presence, name="set_presence"),
    path("availability/", views.team_availability, name="team"),
]
