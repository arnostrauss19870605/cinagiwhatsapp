from django.conf import settings


def site_context(request):
    mode = settings.OUTBOUND_COMMS_MODE
    return {
        "OUTBOUND_COMMS_MODE": mode,
        "OUTBOUND_BANNER": {
            "suppress": ("No messages are being sent", "warning"),
            "allowlist": ("Test mode - only allowlisted numbers receive messages", "info"),
            "live": ("", ""),
        }.get(mode, ("", "")),
        "TAILWIND_CDN": settings.TAILWIND_CDN,
        "SITE_NAME": "Cinagi WhatsApp",
    }
