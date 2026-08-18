import json

from channels.generic.websocket import AsyncWebsocketConsumer

from .events import workspace_group


class InboxConsumer(AsyncWebsocketConsumer):
    """One socket per open inbox. Sends change signals, never content."""

    async def connect(self):
        user = self.scope.get("user")
        if user is None or not user.is_authenticated:
            await self.close()
            return
        self.workspace_id = self.scope["url_route"]["kwargs"]["workspace_id"]
        if not await self._is_member(user, self.workspace_id):
            await self.close()
            return
        self.group = workspace_group(self.workspace_id)
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        if hasattr(self, "group"):
            await self.channel_layer.group_discard(self.group, self.channel_name)

    async def inbox_event(self, event):
        await self.send(text_data=json.dumps(event.get("payload", {})))

    @staticmethod
    async def _is_member(user, workspace_id):
        from channels.db import database_sync_to_async

        from apps.workspaces.models import WorkspaceMembership

        @database_sync_to_async
        def check():
            return WorkspaceMembership.objects.filter(
                user=user, workspace_id=workspace_id, is_active=True
            ).exists()

        return await check()
