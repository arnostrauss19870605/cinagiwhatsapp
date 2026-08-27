from django.urls import path

from . import views

app_name = "events"

urlpatterns = [
    path("<str:token>.ics", views.guest_ics, name="guest_ics"),
    path("register/<int:event_id>/", views.form_rsvp, name="form_rsvp"),
]
