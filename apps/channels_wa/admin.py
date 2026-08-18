from django.contrib import admin

from .models import WhatsAppChannel


@admin.register(WhatsAppChannel)
class WhatsAppChannelAdmin(admin.ModelAdmin):
    list_display = ("display_name", "workspace", "phone_number", "status", "is_active")
    list_filter = ("workspace", "status", "is_active")
    readonly_fields = ("verify_token", "last_verified_at", "last_inbound_at", "templates_synced_at")
