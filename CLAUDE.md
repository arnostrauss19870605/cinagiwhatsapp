# Working on this codebase

House rules and the reasoning behind the structure. Read this before changing anything.

## What this is

A multi-workspace WhatsApp messaging platform. One deployment serves many WhatsApp numbers. A
**workspace** owns one number and everything around it: agents, chats, contacts, saved replies,
templates, working hours. **Nothing crosses between workspaces, ever.**

Stack: Django 6, Postgres 17, Redis, Celery + Beat, Django Channels (ASGI), HTMX 2, Alpine 3,
Tailwind. Docker to Linux.

## The rules that matter

1. **Workspace scoping is not optional.** Tenant models inherit `apps.core.scoping.WorkspaceScopedModel`.
   Views fetch with `Model.objects.for_request(request)` or `scoped_get_or_404(...)`. A denied
   lookup 404s and writes an audit row — it never says "that exists but is not yours".
   `tests/test_workspace_isolation.py` is the guard. Do not weaken it.

2. **One transport class.** Every outbound WhatsApp call goes through
   `apps/channels_wa/messaging/meta_cloud.py`. Nothing else may talk to graph.facebook.com. Adding a
   BSP later means a second class implementing `MessagingChannel` and a change to `get_channel_client`.

3. **The comms guard runs inside the transport, not at the call site.** `OUTBOUND_COMMS_MODE` is
   checked in `_post_message`, so no code path can accidentally bypass it.

4. **The webhook answers 200 immediately.** Meta retries anything else. Verify the signature, enqueue,
   return. Real work happens in the `webhooks` Celery queue. Every inbound `wamid` gets a
   `ProcessedInbound` row before processing, because Meta redelivers.

5. **Credentials live in the database, encrypted** (`apps/core/fields.EncryptedTextField`), never in
   settings. Flat `WHATSAPP_*` settings are what makes a codebase single-tenant.

6. **Realtime sends signals, not content.** `apps/inbox/events.py` broadcasts
   `{"event": ..., "conversation_id": ...}`; the browser refetches the HTMX fragment. Broadcasting
   never raises, and the UI polls every 8 seconds as a fallback.

7. **Status only moves forward.** `Message.advance_status` refuses to let a late `delivered` webhook
   undo `read`.

8. **Write for non-technical users.** Labels, help text and errors are in plain language — "approved
   template", not "HSM"; "this chat has been quiet for more than 24 hours", not "session window
   expired". Wording that might change belongs in `UiCopy` (`{% copy "key" "fallback" %}`).

9. **Audit anything significant** with `apps.core.audit.audit(...)`. It never raises.

10. **No change-narration comments.** Comments explain constraints that are not obvious from the
    code, not what changed or when.

## Layout

```
config/          settings, urls, asgi, celery
apps/core/       audit log, UI copy, feature toggles, scoping, encrypted fields, dashboard
apps/accounts/   custom User
apps/workspaces/ Workspace, membership/roles, business hours, holidays, switcher middleware
apps/channels_wa/WhatsAppChannel, Meta transport, webhook, inbound/outbound, receipts, tasks
apps/contacts/   Contact, external refs (the Xealth seam), consent
apps/inbox/      Conversation, Message, notes, tags, assignment log, consumers, the inbox UI
apps/agents/     AgentProfile, teams, skills, the allocation algorithm
apps/library/    approved templates (synced, read-only) and quick replies (ours)
```

## File modes

Windows has no execute bit and this repo carries `core.filemode=false`, so a shell script that
arrives on a Windows working copy lands in git as `100644` and the Linux server cannot run it. The
push script sets the bit in the **index** (`git update-index --chmod=+x`) for every tracked `*.sh`
before pushing, because that is what actually reaches the server. If you add a shell script, either
run that or expect the server to hit "permission denied".

## Conventions

- Function-based views. HTMX partials under `templates/<app>/partials/`.
- Every button is a real `<form>`; HTMX is progressive enhancement, not a requirement.
- Migrations are committed. `makemigrations --check` before you call a change finished.
- Tests run on sqlite with no Redis and no network: `./scripts/test.sh`.
- `python scripts/smoke.py` renders every page against a throwaway database — run it after template
  changes.

## Known WhatsApp gotchas (already handled — do not "fix" them)

- Template language must match Business Manager exactly: `en_US` ≠ `en`.
- An active template accepts one edit per 24 hours.
- A URL-button suffix containing `:` is silently dropped by WhatsApp; base64url-wrap tokens.
- Free text is only allowed within 24 hours of the customer's last message; after that, an approved
  template is the only way through. Enforced in `Conversation.window_open` and the composer.
- Templates are authored in WhatsApp Manager. This app never creates or edits them.

## Not built yet (by design)

The automation/workflow engine, the AI resolver, reporting, and the Xealth/BIMS integration. The
seams exist: `ContactExternalRef`, `Conversation.automation_state`, the `ai` Celery queue, and the
`integrations` app named in the plan. Build them in phase order; do not scatter early hooks.
