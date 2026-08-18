from django.urls import path

from . import views

urlpatterns = [
    path("", views.inbox, name="inbox"),
    path("snippets/", views.snippet_search, name="snippet_search"),
    path("media/<uuid:pk>/", views.media, name="media"),
    path("ping/", views.ping, name="ping"),
    path("<uuid:pk>/", views.inbox, name="conversation"),
    path("<uuid:pk>/thread/", views.thread, name="thread"),
    path("<uuid:pk>/send/", views.send, name="send"),
    path("<uuid:pk>/send-template/", views.send_template, name="send_template"),
    path("<uuid:pk>/claim/", views.claim, name="claim"),
    path("<uuid:pk>/release/", views.release, name="release"),
    path("<uuid:pk>/resolve/", views.resolve, name="resolve"),
    path("<uuid:pk>/reopen/", views.reopen, name="reopen"),
    path("<uuid:pk>/note/", views.add_note, name="add_note"),
]
