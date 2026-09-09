from django.urls import path

from . import views

urlpatterns = [
    path("", views.overview, name="overview"),
    path("bulk-sends/<int:pk>/", views.bulk_send, name="bulk_send"),
]
