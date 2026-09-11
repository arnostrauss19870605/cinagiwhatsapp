"""Files a bulk send can be taken away as: the recipient list as Excel, and a
one-page summary PDF for whoever asked "how did the send go?"."""

import io
from xml.sax.saxutils import escape

from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from apps.contacts.exports import BLUE, FAINT, GREEN, GREY, H1, H2, INK

BODY = ParagraphStyle("body", fontName="Helvetica", fontSize=9, leading=12, textColor=INK)
LABEL = ParagraphStyle("label", fontName="Helvetica", fontSize=7.5, leading=9, textColor=FAINT)
BIG = ParagraphStyle("big", fontName="Helvetica-Bold", fontSize=18, leading=22, textColor=INK)
SECTION = ParagraphStyle(
    "section", fontName="Helvetica-Bold", fontSize=10, leading=13, textColor=INK, spaceBefore=10, spaceAfter=4
)
CELL = ParagraphStyle("cell", fontName="Helvetica", fontSize=8, leading=10, textColor=INK)
CELL_HEAD = ParagraphStyle("cellhead", fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=colors.white)

COLUMNS = ["Name", "Number", "Status", "Sent", "Delivered", "Read", "Replied", "Reason not delivered"]


def _local(moment):
    return timezone.localtime(moment).strftime("%Y-%m-%d %H:%M") if moment else ""


def _note(message):
    if message.wa_status == "failed":
        return message.failure_explanation
    if message.wa_status == "blocked":
        return (message.wa_error or {}).get("reason", "Held back in test mode")
    return ""


def recipient_rows(messages):
    for message in messages:
        contact = message.conversation.contact
        yield [
            contact.name,
            contact.pretty_number,
            message.get_wa_status_display(),
            _local(message.sent_at or message.created_at),
            _local(message.delivered_at),
            _local(message.read_at),
            "Yes" if getattr(message, "replied", False) else "",
            _note(message),
        ]


def recipients_xlsx(batch, messages, stats):
    """Two sheets: every recipient, and the headline numbers."""
    book = Workbook()
    sheet = book.active
    sheet.title = "Recipients"
    head_fill = PatternFill("solid", fgColor="0065A9")
    sheet.append(COLUMNS)
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = head_fill
        cell.alignment = Alignment(vertical="center")
    for row in recipient_rows(messages):
        sheet.append(row)
    widths = [26, 18, 16, 18, 18, 18, 9, 60]
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{max(sheet.max_row, 1)}"

    summary = book.create_sheet("Summary")
    for label, value in _summary_pairs(batch, stats):
        summary.append([label, value])
    summary.column_dimensions["A"].width = 28
    summary.column_dimensions["B"].width = 40
    for cell in summary["A"]:
        cell.font = Font(bold=True)

    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _summary_pairs(batch, stats):
    return [
        ("Message", batch.template_name),
        ("Audience", batch.audience_label),
        ("Started", _local(batch.started_at or batch.created_at)),
        ("Finished", _local(batch.finished_at)),
        ("Started by", batch.created_by.display_name if batch.created_by else ""),
        ("Values", batch.values_label),
        ("People", batch.recipient_count),
        ("Sent", stats["sent"]),
        ("Delivered", f"{stats['delivered']} ({stats['delivered_pct']}%)"),
        ("Read", f"{stats['read']} ({stats['read_pct']}%)"),
        ("Replied", f"{stats['replied']} ({stats['replied_pct']}%)"),
        ("Not delivered", stats["failed"]),
        ("Held back in test mode", stats["blocked"]),
    ]


def summary_pdf(target, batch, stats, messages, failure_rows, *, exported_by=None):
    """A one-to-two page report: headline tiles, the funnel, why things failed, and the list."""
    doc = SimpleDocTemplate(
        target, pagesize=A4,
        leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm, bottomMargin=16 * mm,
        title=f"Bulk send report - {batch.template_name}",
    )
    width = doc.width
    exported_at = timezone.localtime(timezone.now())
    footer_text = (
        f"Exported {exported_at.day} {exported_at:%B %Y %H:%M}"
        + (f" by {exported_by.display_name}" if exported_by is not None else "")
        + f"  ·  {batch.workspace.name}  ·  Cinagi (Pty) Ltd"
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

    started = batch.started_at or batch.created_at
    header = Table(
        [[Paragraph("Cinagi WhatsApp — bulk send report", H1)],
         [Paragraph(
             f"{escape(batch.template_name)} · to {escape(batch.audience_label)} · "
             f"{_local(started)}" + (f" · by {escape(batch.created_by.display_name)}" if batch.created_by else ""),
             H2,
         )]],
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

    tiles = [
        ("People", str(batch.recipient_count)),
        ("Sent", str(stats["sent"])),
        ("Delivered", f"{stats['delivered_pct']}%"),
        ("Read", f"{stats['read_pct']}%"),
        ("Replied", str(stats["replied"])),
        ("Not delivered", str(stats["failed"])),
    ]
    tile_table = Table(
        [[Table([[Paragraph(label, LABEL)], [Paragraph(value, BIG)]], colWidths=[width / 6 - 6]) for label, value in tiles]],
        colWidths=[width / 6] * 6,
    )
    tile_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), GREY),
        ("ROUNDEDCORNERS", [6, 6, 6, 6]),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story += [tile_table, Spacer(1, 4 * mm)]

    story.append(Paragraph("From sent to replied", SECTION))
    funnel_rows = []
    for label, value, pct in stats["funnel"]:
        bar = Table([[""]], colWidths=[max(2, width * 0.55 * pct / 100)], rowHeights=[6])
        bar.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), BLUE), ("ROUNDEDCORNERS", [3, 3, 3, 3])]))
        funnel_rows.append([Paragraph(label, BODY), bar, Paragraph(f"{value} ({pct}%)", BODY)])
    funnel = Table(funnel_rows, colWidths=[width * 0.18, width * 0.58, width * 0.24])
    funnel.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    story.append(funnel)

    if batch.values:
        story.append(Paragraph("Message values", SECTION))
        story.append(Paragraph(escape(batch.values_label), BODY))

    story.append(Paragraph("Why messages were not delivered", SECTION))
    if failure_rows:
        for row in failure_rows:
            story.append(Paragraph(f"<b>{row['count']}</b> — {escape(row['reason'])}", BODY))
    else:
        story.append(Paragraph("Every message that was sent reached a phone.", BODY))

    story.append(Paragraph("Everyone this went to", SECTION))
    table_rows = [[Paragraph(c, CELL_HEAD) for c in ["Name", "Number", "Status", "Read", "Replied", "Note"]]]
    for row in recipient_rows(messages):
        name, number, status, _sent, _delivered, read, replied, note = row
        table_rows.append([Paragraph(escape(str(c)), CELL) for c in [name, number, status, read, replied, note]])
    listing = Table(
        table_rows,
        colWidths=[width * 0.22, width * 0.15, width * 0.13, width * 0.14, width * 0.08, width * 0.28],
        repeatRows=1,
    )
    listing.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BLUE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, GREY]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#E2E8F0")),
    ]))
    story.append(listing)

    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    return target
