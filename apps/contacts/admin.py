from django.contrib import admin

from .models import Contact, ContactConsent, ContactExternalRef


class ExternalRefInline(admin.TabularInline):
    model = ContactExternalRef
    extra = 0


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ("name", "wa_id", "workspace", "last_seen_at", "is_blocked")
    list_filter = ("workspace", "is_blocked")
    search_fields = ("wa_id", "display_name", "profile_name")
    inlines = [ExternalRefInline]


admin.site.register(ContactConsent)
