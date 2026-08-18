from django.urls import path

from .consumers import InboxConsumer

websocket_urlpatterns = [
    path("ws/inbox/<int:workspace_id>/", InboxConsumer.as_asgi()),
]
