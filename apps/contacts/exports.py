"""The chat-history PDF: a document worth handing to a client or an auditor.

Laid out like the chat itself - customer on the left in grey, us on the right
in Cinagi blue - with a branded header and numbered pages. Colours match
cinagi.co.za: blue #0065A9, green #76BD4C.
"""

from xml.sax.saxutils import escape

from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

BLUE = colors.HexColor("#0065A9")
BLUE_TINT = colors.HexColor("#E5F1F9")
GREEN = colors.HexColor("#76BD4C")
GREY = colors.HexColor("#F1F5F9")
INK = colors.HexColor("#1E293B")
FAINT = colors.HexColor("#64748B")

BODY = ParagraphStyle("body", fontName="Helvetica", fontSize=9.5, leading=13, textColor=INK)
META = ParagraphStyle("meta", fontName="Helvetica", fontSize=7, leading=9, textColor=FAINT)
NOTE = ParagraphStyle("note", parent=BODY, textColor=colors.HexColor("#92400E"))
DAY = ParagraphStyle(
    "day", fontName="Helvetica-Bold", fontSize=8, leading=10,
    textColor=FAINT, alignment=1, spaceBefore=8, spaceAfter=4,
)
H1 = ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=16, leading=20, textColor=colors.white)
H2 = ParagraphStyle("h2", fontName="Helvetica", fontSize=9, leading=12, textColor=colors.HexColor("#CFE8FA"))


def _text(value):
    return escape(value or "").replace("\n", "<br/>")


def _bubble(width, body_html, meta_html, *, align_right, background, style=BODY):
    inner = Table(
        [[Paragraph(body_html, style)], [Paragraph(meta_html, META)]],
        colWidths=[width * 0.62],
    )
    inner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), background),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (0, 0), 6),
        ("BOTTOMPADDING", (0, 1), (0, 1), 5),
        ("TOPPADDING", (0, 1), (0, 1), 1),
        ("BOTTOMPADDING", (0, 0), (0, 0), 1),
        ("ROUNDEDCORNERS", [6, 6, 6, 6]),
        ("LINEBELOW", (0, -1), (-1, -1), 0, background),
    ]))
    row = [[inner, ""]] if not align_right else [["", inner]]
    shell = Table(row, colWidths=[width * (0.36 if align_right else 0.64),
                                  width * (0.64 if align_right else 0.36)])
    shell.setStyle(TableStyle([
        ("ALIGN", (0, 0), (0, 0), "LEFT"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    return shell


def chat_pdf(target, workspace, contact, history, *, exported_by=None):
    """Write the PDF into `target` (any file-like, incl. an HttpResponse)."""
    doc = SimpleDocTemplate(
        target, pagesize=A4,
        leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm, bottomMargin=16 * mm,
        title=f"WhatsApp history - {contact.name}",
    )
    width = doc.width

    exported_at = timezone.localtime(timezone.now())
    footer_text = (
        f"Exported {exported_at.day} {exported_at:%B %Y %H:%M}"
        + (f" by {exported_by.display_name}" if exported_by is not None else "")
        + f"  ·  {workspace.name}  ·  Cinagi (Pty) Ltd"
    )

    def decorate(canvas, _doc):
        canvas.saveState()
        canvas.setFillColor(BLUE)
        canvas.rect(0, A4[1] - 6, A4[0], 6, stroke=0, fill=1)
        canvas.setFillColor(GREEN)
        canvas.rect(0, A4[1] - 8, A4[0], 2, stroke=0, fill=1)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(FAINT)
        canvas.drawString(16 * mm, 8 * mm, footer_text)
        canvas.drawRightString(A4[0] - 16 * mm, 8 * mm, f"Page {canvas.getPageNumber()}")
        canvas.restoreState()

    header = Table(
        [[Paragraph("Cinagi WhatsApp — chat history", H1)],
         [Paragraph(f"{escape(contact.name)} · {contact.pretty_number} · "
                    f"{len(history)} message{'s' if len(history) != 1 else ''}", H2)]],
        colWidths=[width],
    )
    header.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BLUE),
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
        ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING", (0, 0), (0, 0), 12),
        ("BOTTOMPADDING", (0, 1), (0, 1), 12),
        ("ROUNDEDCORNERS", [8, 8, 8, 8]),
    ]))

    story = [header, Spacer(1, 6 * mm)]

    current_day = None
    for message in history:
        moment = timezone.localtime(message.created_at)
        day = moment.date()
        if day != current_day:
            current_day = day
            story.append(Paragraph(f"{day.day} {day:%B %Y}", DAY))
            story.append(HRFlowable(width="40%", color=GREY, thickness=1,
                                    spaceBefore=0, spaceAfter=4, hAlign="CENTER"))

        body = _text(message.body) or f"<i>[{escape(message.media_filename or message.kind)}]</i>"
        if message.payload.get("note"):
            author = message.author.display_name if message.author_id else "Team"
            story.append(_bubble(
                width, f"<b>Internal note</b><br/>{body}",
                f"{escape(author)} · {moment:%H:%M}",
                align_right=False, background=colors.HexColor("#FEF3C7"), style=NOTE,
            ))
            continue

        if message.is_inbound:
            story.append(_bubble(
                width, body, f"{escape(contact.name)} · {moment:%H:%M}",
                align_right=False, background=GREY,
            ))
        else:
            sender = message.author.display_name if message.author_id else message.get_actor_display()
            tag = " · template" if message.kind == "template" else ""
            status = f" · {message.wa_status}" if message.wa_status else ""
            story.append(_bubble(
                width, body, f"{escape(sender)}{tag}{status} · {moment:%H:%M}",
                align_right=True, background=BLUE_TINT,
            ))

    if not history:
        story.append(Paragraph("No messages with this person yet.", BODY))

    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
