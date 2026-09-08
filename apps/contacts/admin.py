from django.contrib import admin

from .models import Audience, Contact, ContactConsent, ContactExternalRef


class ExternalRefInline(admin.TabularInline):
    model = ContactExternalRef
    extra = 0


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ("name", "wa_id", "workspace", "last_seen_at", "is_blocked")
    list_filter = ("workspace", "is_blocked", "audiences")
    search_fields = ("wa_id", "display_name", "profile_name")
    inlines = [ExternalRefInline]


@admin.register(Audience)
class AudienceAdmin(admin.ModelAdmin):
    list_display = ("name", "workspace", "description", "member_count")
    list_filter = ("workspace",)
    search_fields = ("name", "description")
    filter_horizontal = ("contacts",)

    @admin.display(description="Members")
    def member_count(self, audience):
        return audience.contacts.count()


admin.site.register(ContactConsent)
