from django.contrib import admin

from .models import AssignmentLog, Conversation, InternalNote, Message, ProcessedInbound, Tag


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0
    fields = ("created_at", "direction", "kind", "body", "wa_status")
    readonly_fields = fields


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("contact", "workspace", "status", "assigned_to", "last_activity_at")
    list_filter = ("workspace", "status")
    inlines = [MessageInline]


admin.site.register([Tag, InternalNote, AssignmentLog, ProcessedInbound])
