from django.contrib import messages as flash
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.core.audit import audit
from apps.core.scoping import require_role, scoped_get_or_404
from apps.workspaces.models import WorkspaceMembership

from .forms import ChannelConnectForm, TestMessageForm
from .models import WhatsAppChannel



@login_required
def channel_list(request):
    if request.workspace is None:
        return redirect("workspaces:list")
    return render(
        request,
        "channels_wa/list.html",
        {"channels": WhatsAppChannel.objects.for_request(request)},
    )


@login_required
def connect(request):
    """Step 1 of the wizard: paste the four values from Meta."""
    if request.workspace is None:
        return redirect("workspaces:list")
    require_role(request, *WorkspaceMembership.MANAGE_ROLES)

    if request.method == "POST":
        form = ChannelConnectForm(request.POST)
        if form.is_valid():
            channel = form.save(commit=False)
            channel.workspace = request.workspace
            channel.is_default = not WhatsAppChannel.objects.for_request(request).exists()
            channel.save()
            audit("channel.created", request=request, target=channel, name=channel.display_name)
            return redirect("channels_wa:verify", pk=channel.pk)
    else:
        form = ChannelConnectForm()
    return render(request, "channels_wa/connect.html", {"form": form})


@login_required
def edit(request, pk):
    require_role(request, *WorkspaceMembership.MANAGE_ROLES)
    channel = scoped_get_or_404(WhatsAppChannel, request, pk=pk)
    if request.method == "POST":
        form = ChannelConnectForm(request.POST, instance=channel)
        if form.is_valid():
            form.save()
            audit("channel.updated", request=request, target=channel, fields=list(form.changed_data))
            flash.success(request, "Saved. Run the check again to confirm it still works.")
            return redirect("channels_wa:verify", pk=channel.pk)
    else:
        form = ChannelConnectForm(instance=channel)
    return render(request, "channels_wa/connect.html", {"form": form, "channel": channel})


@login_required
def verify(request, pk):
    """Step 2: prove the credentials work, then send a real test message."""
    require_role(request, *WorkspaceMembership.MANAGE_ROLES)
    channel = scoped_get_or_404(WhatsAppChannel, request, pk=pk)
    profile, error, test_form, test_result = None, None, TestMessageForm(), None

    if request.method == "POST" and request.POST.get("action") == "check":
        try:
            profile = channel.client().fetch_number_profile()
            channel.status = WhatsAppChannel.Status.CONNECTED
            channel.status_detail = ""
            channel.quality_rating = profile.get("quality_rating", "") or ""
            channel.messaging_limit = profile.get("messaging_limit_tier", "") or ""
            channel.phone_number = channel.phone_number or profile.get("display_phone_number", "")
            channel.last_verified_at = timezone.now()
            channel.save()
            audit("channel.verified", request=request, target=channel)
            flash.success(request, "Connected. WhatsApp recognised this number.")
        except Exception as exc:
            error = getattr(exc, "friendly", None) or "We could not reach WhatsApp with those details."
            channel.status = WhatsAppChannel.Status.ERROR
            channel.status_detail = error
            channel.save(update_fields=["status", "status_detail"])
            audit("channel.verify_failed", request=request, target=channel, error=str(exc)[:200])

    elif request.method == "POST" and request.POST.get("action") == "test":
        test_form = TestMessageForm(request.POST)
        if test_form.is_valid():
            number = test_form.cleaned_data["to_number"]
            # Send through the normal outbound path so the message is stored with
            # its wamid. WhatsApp accepts a send (HTTP 200) and only reports
            # failure later, on the status webhook - without a stored message
            # there is nothing for that callback to attach to, and the send
            # looks successful forever.
            from apps.channels_wa.outbound import send_text
            from apps.contacts.models import Contact
            from apps.inbox.models import Conversation, Message

            contact, _ = Contact.objects.get_or_create(
                workspace=request.workspace,
                wa_id=number,
                defaults={"display_name": "Connection test"},
            )
            conversation = (
                Conversation.objects.for_request(request)
                .filter(contact=contact, status__in=Conversation.OPEN_STATUSES)
                .first()
            ) or Conversation.objects.create(
                workspace=request.workspace,
                channel=channel,
                contact=contact,
                status=Conversation.Status.BOT,
                subject="Connection test",
            )
            message = send_text(
                conversation,
                f"Test message from {request.workspace.name}. If you can read this, "
                f"{channel.display_name} is connected correctly.",
                author=request.user,
                actor=Message.Actor.BOT,
                payload={"connection_test": True},
            )
            if message.wa_status == Message.Status.BLOCKED:
                test_result = ("warning", message.wa_error.get("reason", ""))
            elif message.wa_status == Message.Status.FAILED:
                test_result = ("error", message.wa_error.get("reason", "WhatsApp rejected it."))
            else:
                test_result = (
                    "ok",
                    f"WhatsApp accepted it for +{number}. Refresh this page in a few "
                    "seconds to see whether it was actually delivered.",
                )
            request.session["last_test_message_id"] = str(message.pk)
            audit("channel.test_send", request=request, target=channel, outcome=test_result[0])

    # The real answer to "did it arrive" comes from the status webhook, so show
    # the stored status of the last test rather than what the API said at send time.
    last_test = None
    last_test_id = request.session.get("last_test_message_id")
    if last_test_id:
        from apps.inbox.models import Message

        last_test = (
            Message.objects.for_request(request).filter(pk=last_test_id).first()
        )

    return render(
        request,
        "channels_wa/verify.html",
        {
            "channel": channel,
            "profile": profile,
            "error": error,
            "test_form": test_form,
            "test_result": test_result,
            "last_test": last_test,
        },
    )


@login_required
def sync_templates_now(request, pk):
    require_role(request, *WorkspaceMembership.MANAGE_ROLES)
    channel = scoped_get_or_404(WhatsAppChannel, request, pk=pk)
    from .tasks import sync_templates

    try:
        count = sync_templates(channel.pk)
        flash.success(request, f"Loaded {count} approved template(s) from WhatsApp.")
    except Exception:
        flash.error(request, "We could not load your templates. Check the Business Account ID.")
    return redirect("library:templates")


@login_required
def toggle_active(request, pk):
    require_role(request, *WorkspaceMembership.MANAGE_ROLES)
    channel = scoped_get_or_404(WhatsAppChannel, request, pk=pk)
    channel.is_active = not channel.is_active
    channel.save(update_fields=["is_active"])
    audit("channel.toggled", request=request, target=channel, active=channel.is_active)
    flash.success(request, "Number switched " + ("on." if channel.is_active else "off."))
    return redirect("channels_wa:list")
