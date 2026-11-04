from io import BytesIO
from pathlib import Path

from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from xml.sax.saxutils import escape


INK = colors.HexColor('#172033')
MUTED = colors.HexColor('#64748B')
BLUE = colors.HexColor('#1D4ED8')
PALE_GRAY = colors.HexColor('#F8FAFC')
GRID = colors.HexColor('#E2E8F0')


def _register_invoice_font():
    font_candidates = [
        (Path('C:/Windows/Fonts/Nirmala.ttc'), 0),
        (Path('/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf'), None),
        (Path('/usr/share/fonts/truetype/freefont/FreeSans.ttf'), None),
    ]
    for font_path, subfont_index in font_candidates:
        if not font_path.is_file():
            continue
        try:
            pdfmetrics.registerFont(
                TTFont('InvoiceUnicode', str(font_path), subfontIndex=subfont_index)
            )
        except (OSError, ValueError):
            continue
        return 'InvoiceUnicode'
    return 'Helvetica'


def _paragraph(value, style):
    text = escape(str(value or '—')).replace('\n', '<br/>')
    return Paragraph(text, style)


def _paragraph_markup(markup, style):
    return Paragraph(markup, style)


def build_order_invoice(order, site_settings):
    buffer = BytesIO()
    font_name = _register_invoice_font()
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name='InvoiceBrand',
        parent=styles['Title'],
        fontName=font_name,
        fontSize=21,
        leading=25,
        textColor=INK,
        alignment=TA_LEFT,
        spaceAfter=2,
    ))
    styles.add(ParagraphStyle(
        name='InvoiceSubtitle',
        parent=styles['Normal'],
        fontName=font_name,
        fontSize=8.5,
        leading=12,
        textColor=MUTED,
    ))
    styles.add(ParagraphStyle(
        name='InvoiceHeading',
        parent=styles['Heading2'],
        fontName=font_name,
        fontSize=10,
        leading=13,
        textColor=INK,
        spaceAfter=5,
    ))
    styles.add(ParagraphStyle(
        name='InvoiceBody',
        parent=styles['BodyText'],
        fontName=font_name,
        fontSize=8.5,
        leading=12,
        textColor=INK,
    ))
    styles.add(ParagraphStyle(
        name='InvoiceSmall',
        parent=styles['BodyText'],
        fontName=font_name,
        fontSize=7.5,
        leading=10,
        textColor=MUTED,
    ))
    styles.add(ParagraphStyle(
        name='InvoiceTableHeader',
        parent=styles['BodyText'],
        fontName=font_name,
        fontSize=7.5,
        leading=9,
        textColor=colors.white,
    ))
    styles.add(ParagraphStyle(
        name='InvoiceTableHeaderRight',
        parent=styles['InvoiceTableHeader'],
        alignment=TA_RIGHT,
    ))
    styles.add(ParagraphStyle(
        name='InvoiceTableHeaderCenter',
        parent=styles['InvoiceTableHeader'],
        alignment=TA_CENTER,
    ))
    styles.add(ParagraphStyle(
        name='InvoiceTableAmount',
        parent=styles['InvoiceBody'],
        alignment=TA_RIGHT,
    ))
    styles.add(ParagraphStyle(
        name='InvoiceTableQuantity',
        parent=styles['InvoiceBody'],
        alignment=TA_CENTER,
    ))
    styles.add(ParagraphStyle(
        name='InvoiceTotalLabel',
        parent=styles['InvoiceBody'],
        alignment=TA_RIGHT,
    ))
    styles.add(ParagraphStyle(
        name='InvoiceTotalValue',
        parent=styles['InvoiceBody'],
        alignment=TA_RIGHT,
    ))
    page_width, _ = A4
    content_width = page_width - (36 * mm)
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=18 * mm,
        title=f'Invoice - Order #{order.display_order_number}',
        author=site_settings.brand_name,
    )
    story = []
    brand = _paragraph(site_settings.brand_name, styles['InvoiceBrand'])
    brand_details = _paragraph_markup(
        f'{escape(site_settings.address)}<br/>'
        f'{escape(site_settings.phone)} &nbsp; | &nbsp; {escape(site_settings.email)}',
        styles['InvoiceSubtitle'],
    )
    invoice_title = _paragraph('INVOICE', styles['InvoiceHeading'])
    invoice_reference = _paragraph_markup(
        f'<b>Order</b> #{order.display_order_number}<br/>'
        f'<b>Date</b> {timezone.localtime(order.created_at):%b %d, %Y}<br/>'
        f'<b>Status</b> {order.get_status_display()}',
        styles['InvoiceBody'],
    )
    header = Table(
        [[brand, invoice_title], [brand_details, invoice_reference]],
        colWidths=[content_width * 0.62, content_width * 0.38],
    )
    header.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('SPAN', (1, 0), (1, 0)),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    story.extend([header, Spacer(1, 7 * mm), HRFlowable(
        width='100%', thickness=1, color=GRID, spaceAfter=6 * mm,
    )])

    customer_lines = [
        f'<b>{escape(order.full_name)}</b>',
        escape(order.phone),
    ]
    if order.email:
        customer_lines.append(escape(order.email))
    customer_lines.extend([
        escape(order.address),
        ' / '.join(
            escape(value) for value in (order.thana, order.zilla, order.city) if value
        ),
    ])
    customer_details = _paragraph_markup('<br/>'.join(customer_lines), styles['InvoiceBody'])
    if order.payment_amount is not None:
        amount_paid = f'BDT {order.payment_amount:,.2f}'
    elif order.payment_method == 'cod':
        amount_paid = 'BDT 0.00'
    else:
        amount_paid = '—'
    payment_details = _paragraph_markup(
        f'<b>Method</b> {escape(order.get_payment_method_display())}<br/>'
        f'<b>Payment number</b> {escape(order.payment_number or "—")}<br/>'
        f'<b>Transaction reference</b> {escape(order.transaction_reference or "—")}<br/>'
        f'<b>Amount paid</b> {amount_paid}',
        styles['InvoiceBody'],
    )
    party_table = Table(
        [[
            _paragraph('BILL TO', styles['InvoiceHeading']),
            _paragraph('PAYMENT', styles['InvoiceHeading']),
        ], [customer_details, payment_details]],
        colWidths=[content_width / 2, content_width / 2],
    )
    party_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), PALE_GRAY),
        ('BOX', (0, 0), (-1, -1), 0.6, GRID),
        ('INNERGRID', (0, 0), (-1, -1), 0.4, GRID),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 10),
        ('RIGHTPADDING', (0, 0), (-1, -1), 10),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    story.extend([party_table, Spacer(1, 8 * mm)])

    item_rows = [[
        _paragraph('ITEM', styles['InvoiceTableHeader']),
        _paragraph('DETAILS', styles['InvoiceTableHeader']),
        _paragraph('QTY', styles['InvoiceTableHeaderCenter']),
        _paragraph('UNIT PRICE (BDT)', styles['InvoiceTableHeaderRight']),
        _paragraph('AMOUNT (BDT)', styles['InvoiceTableHeaderRight']),
    ]]
    for item in order.items.all():
        details = []
        if item.selected_size:
            details.append(f'Size: {escape(item.selected_size)}')
        if item.selected_color:
            details.append(f'Color: {escape(item.selected_color)}')
        item_rows.append([
            _paragraph(item.product_name, styles['InvoiceBody']),
            _paragraph_markup('<br/>'.join(details) or '—', styles['InvoiceSmall']),
            _paragraph(str(item.quantity), styles['InvoiceTableQuantity']),
            _paragraph(f'{item.unit_price:,.2f}', styles['InvoiceTableAmount']),
            _paragraph(f'{item.line_total:,.2f}', styles['InvoiceTableAmount']),
        ])

    item_widths = [
        content_width * 0.30,
        content_width * 0.22,
        content_width * 0.08,
        content_width * 0.20,
        content_width * 0.20,
    ]
    items_table = Table(item_rows, colWidths=item_widths, repeatRows=1, hAlign='LEFT')
    items_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), INK),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, PALE_GRAY]),
        ('LINEBELOW', (0, 0), (-1, 0), 0.6, INK),
        ('LINEBELOW', (0, 1), (-1, -1), 0.4, GRID),
        ('LEFTPADDING', (0, 0), (-1, -1), 7),
        ('RIGHTPADDING', (0, 0), (-1, -1), 7),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    story.append(items_table)

    totals_table = Table(
        [
            [_paragraph('Subtotal', styles['InvoiceTotalLabel']),
             _paragraph(f'BDT {order.subtotal:,.2f}', styles['InvoiceTotalValue'])],
            [_paragraph('Shipping', styles['InvoiceTotalLabel']),
             _paragraph(f'BDT {order.shipping_charge:,.2f}', styles['InvoiceTotalValue'])],
            [_paragraph('Cash discount', styles['InvoiceTotalLabel']),
             _paragraph(
                 f'- BDT {order.cash_discount:,.2f}',
                 styles['InvoiceTotalValue'],
             )],
            [_paragraph_markup('<b>Total</b>', styles['InvoiceTotalLabel']),
             _paragraph_markup(
                 f'<b>BDT {order.total:,.2f}</b>',
                 styles['InvoiceTotalValue'],
             )],
        ],
        colWidths=[content_width * 0.68, content_width * 0.32],
        hAlign='RIGHT',
    )
    totals_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LINEABOVE', (0, 3), (-1, 3), 0.6, INK),
        ('BACKGROUND', (0, 3), (-1, 3), colors.HexColor('#EFF6FF')),
    ]))
    story.extend([Spacer(1, 5 * mm), totals_table])

    if order.note:
        story.extend([
            Spacer(1, 7 * mm),
            _paragraph('CUSTOMER NOTE', styles['InvoiceHeading']),
            _paragraph(order.note, styles['InvoiceBody']),
        ])
    story.extend([
        Spacer(1, 12 * mm),
        _paragraph('Thank you for your order.', styles['InvoiceSmall']),
    ])

    def draw_footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(GRID)
        canvas.line(18 * mm, 13 * mm, page_width - (18 * mm), 13 * mm)
        canvas.setFont(font_name, 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(18 * mm, 8 * mm, site_settings.brand_name)
        canvas.drawRightString(page_width - (18 * mm), 8 * mm, f'Page {doc.page}')
        canvas.restoreState()

    document.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)
    return buffer.getvalue()
