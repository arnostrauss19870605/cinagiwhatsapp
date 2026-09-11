from django.urls import path

from . import views

urlpatterns = [
    path("templates/", views.templates, name="templates"),
    path("templates/new/", views.draft_edit, name="draft_create"),
    path("templates/drafts/<int:pk>/", views.draft_edit, name="draft_edit"),
    path("templates/drafts/<int:pk>/new-version/", views.draft_clone, name="draft_clone"),
    path("templates/drafts/<int:pk>/delete/", views.draft_delete, name="draft_delete"),
    path("bulk-send/", views.bulk_send, name="bulk_send"),
    path("replies/", views.snippets, name="snippets"),
    path("replies/new/", views.snippet_edit, name="snippet_create"),
    path("replies/<int:pk>/", views.snippet_edit, name="snippet_edit"),
    path("replies/<int:pk>/delete/", views.snippet_delete, name="snippet_delete"),
]
