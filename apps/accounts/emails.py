"""The two emails a login needs: the sign-in code, and the welcome note.

Both go through Microsoft Graph, the same route the event invitations take.
When Graph is not configured (a laptop, the test suite) nothing is sent: the
code is logged so a developer can still get in, and the caller is told.
"""

import logging

from django.conf import settings
from django.urls import reverse
from django.utils.html import escape

from apps.integrations.msgraph import GraphClient, GraphError

logger = logging.getLogger(__name__)

SHELL = """\
<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
            max-width:560px;margin:0 auto;color:#0f172a;line-height:1.55">
  <div style="background:#0065A9;padding:24px 32px;border-radius:16px 16px 0 0">
    <p style="margin:0;color:#ffffff;font-size:13px;letter-spacing:.08em;text-transform:uppercase">Cinagi WhatsApp</p>
    <h1 style="margin:6px 0 0;color:#ffffff;font-size:22px">{heading}</h1>
  </div>
  <div style="border:1px solid #e2e8f0;border-top:0;border-radius:0 0 16px 16px;padding:28px 32px">
    {body}
    <p style="margin:28px 0 0;padding-top:16px;border-top:1px solid #e2e8f0;font-size:12px;color:#94a3b8">
      This email was sent by the Cinagi WhatsApp inbox. If you were not expecting it, you can ignore it.
    </p>
  </div>
</div>
"""

CODE_BODY = """\
<p style="margin:0 0 16px">Hi {first_name},</p>
<p style="margin:0 0 16px">Here is your sign-in code. It works for the next {minutes} minutes.</p>
<p style="margin:0 0 24px;font-size:32px;letter-spacing:.3em;font-weight:700;color:#0065A9">{code}</p>
<p style="margin:0;font-size:14px;color:#475569">
  If you did not ask for this, nobody can use it without access to this mailbox. No action is needed.
</p>
"""

WELCOME_BODY = """\
<p style="margin:0 0 16px">Hi {first_name},</p>
<p style="margin:0 0 16px">{inviter} has set you up on the Cinagi WhatsApp inbox.</p>
<p style="margin:0 0 8px"><strong>How to sign in</strong></p>
<p style="margin:0 0 16px">
  Go to the sign-in page, enter this email address (<strong>{email}</strong>) and we will email you
  a six-digit code. There is no password to remember.
</p>
<p style="margin:0 0 24px">
  <a href="{login_url}" style="background:#0065A9;color:#ffffff;text-decoration:none;padding:12px 24px;
     border-radius:10px;font-weight:600;display:inline-block">Sign in</a>
</p>
<p style="margin:0 0 8px"><strong>What you can do</strong></p>
{access}
"""


def login_url():
    return f"{settings.PUBLIC_BASE_URL}{reverse('accounts:login')}"


def _client():
    client = GraphClient()
    return client if client.configured else None


def send_login_code(user, code):
    """Email the code. Returns True if it went, False if email is not set up."""
    client = _client()
    if client is None:
        logger.warning("email not configured; sign-in code for %s is %s", user.email, code)
        return False
    html = SHELL.format(
        heading="Your sign-in code",
        body=CODE_BODY.format(
            first_name=escape(user.first_name or "there"),
            minutes=settings.LOGIN_CODE_MINUTES,
            code=code,
        ),
    )
    try:
        client.send_mail(user.email, f"{code} is your Cinagi sign-in code", html, save_to_sent=False)
    except GraphError as exc:
        logger.warning("sign-in code email failed for %s: %s", user.email, exc.friendly)
        raise
    return True


def send_welcome(user, inviter, memberships):
    """Tell a new person how to get in and what they have access to."""
    client = _client()
    if client is None:
        logger.warning("email not configured; welcome email for %s not sent", user.email)
        return False
    if memberships:
        items = "".join(
            f"<li>{escape(m.workspace.name)} as {escape(m.get_role_display().lower())}"
            + (
                " (" + ", ".join(escape(label.lower()) for field, label in m.RIGHTS if getattr(m, field)) + ")"
                if any(getattr(m, field) for field, _ in m.RIGHTS)
                else ""
            )
            + "</li>"
            for m in memberships
        )
        access = f'<ul style="margin:0 0 16px;padding-left:20px">{items}</ul>'
    else:
        access = '<p style="margin:0 0 16px">You have not been given a number yet; an administrator will add one.</p>'
    html = SHELL.format(
        heading="Welcome to the Cinagi WhatsApp inbox",
        body=WELCOME_BODY.format(
            first_name=escape(user.first_name or "there"),
            inviter=escape(inviter.display_name if inviter else "An administrator"),
            email=escape(user.email),
            login_url=escape(login_url()),
            access=access,
        ),
    )
    client.send_mail(user.email, "Your Cinagi WhatsApp login", html)
    return True
