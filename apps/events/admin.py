from django.contrib import admin

from .models import Event, Guest, JourneyEvent


class GuestInline(admin.TabularInline):
    model = Guest
    extra = 0
    fields = ("first_name", "last_name", "email", "msisdn", "rsvp_status", "stage")
    readonly_fields = fields
    show_change_link = True


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("name", "starts_at", "venue", "capacity", "seats_taken", "is_active")
    inlines = [GuestInline]


@admin.register(Guest)
class GuestAdmin(admin.ModelAdmin):
    list_display = (
        "full_name", "company", "msisdn", "rsvp_status", "stage", "guest_number", "event",
    )
    list_filter = ("event", "rsvp_status", "stage", "segment")
    search_fields = ("first_name", "last_name", "email", "msisdn", "company")
    readonly_fields = ("invite_token", "ticket_token")


@admin.register(JourneyEvent)
class JourneyEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "guest", "step", "detail")
    list_filter = ("step",)
