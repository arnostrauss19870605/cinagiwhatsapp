from django.urls import path

from . import views

urlpatterns = [
    path("templates/", views.templates, name="templates"),
    path("replies/", views.snippets, name="snippets"),
    path("replies/new/", views.snippet_edit, name="snippet_create"),
    path("replies/<int:pk>/", views.snippet_edit, name="snippet_edit"),
    path("replies/<int:pk>/delete/", views.snippet_delete, name="snippet_delete"),
]
