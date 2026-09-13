"""Generate a paginated results report in memory, without temporary files."""
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image

LOGO_PATH = Path(__file__).resolve().parent / 'static' / 'images' / 'logo1.png'


def results_pdf(quiz, results):
    stream = BytesIO()
    document = SimpleDocTemplate(stream, pagesize=landscape(A4), rightMargin=28, leftMargin=28,
                                 topMargin=32, bottomMargin=40, title=f"C2C - {quiz['title']}")
    styles = getSampleStyleSheet()
    
    # Custom Styles
    cell = ParagraphStyle('Cell', fontName='Helvetica', fontSize=8, leading=11, wordWrap='CJK')
    header_cell = ParagraphStyle('HeaderCell', parent=cell, textColor=colors.white, fontName='Helvetica-Bold')
    
    title_style = ParagraphStyle('TitleCentered', parent=styles['Heading1'], alignment=1, 
                                 textColor=colors.HexColor('#123453'), spaceAfter=2)
    subtitle_style = ParagraphStyle('SubtitleCentered', parent=styles['Normal'], alignment=1, 
                                    textColor=colors.HexColor('#52637c'), fontSize=10, spaceAfter=14)
    college_style = ParagraphStyle('CollegeCentered', parent=styles['Normal'], alignment=1, 
                                   fontSize=11, textColor=colors.HexColor('#123453'), fontName='Helvetica-Bold')
    info_style = ParagraphStyle('Info', parent=styles['Normal'], fontSize=9, textColor=colors.HexColor('#333333'), leading=12)

    def paragraph(value, style=cell):
        # Escape handles < > &, but we must ensure no double escaping if it's already safe.
        return Paragraph(escape(str(value)).replace('&amp;amp;', '&amp;'), style)

    content = []
    
    # HEADER
    header_table_data = []
    if LOGO_PATH.exists():
        header_table_data.append([Image(str(LOGO_PATH), width=100, height=100, kind='proportional')])
    
    header_table_data.append([Paragraph('HINDUSTHAN COLLEGE OF TECHNOLOGY<br/>Salem – Tamil Nadu', college_style)])
    
    ht = Table(header_table_data, colWidths=[document.width])
    ht.setStyle(TableStyle([('ALIGN', (0,0), (-1,-1), 'CENTER'), ('VALIGN', (0,0), (-1,-1), 'MIDDLE'), ('BOTTOMPADDING', (0,0), (-1,-1), 2)]))
    content.append(ht)
    
    content.append(Spacer(1, 10))
    content.append(Paragraph('COLLEGE TO CAMPUS (C2C)', title_style))
    content.append(Paragraph('Smart Quiz &amp; Assessment Platform | Results Report', subtitle_style))
    content.append(Spacer(1, 10))
    
    # QUIZ INFO
    info_data = [
        [Paragraph(f"<b>Quiz Name:</b> {escape(quiz['title'])}", info_style),
         Paragraph(f"<b>Quiz Code:</b> {quiz['code']}", info_style)],
        [Paragraph(f"<b>Description:</b> {escape(quiz['description'] or 'N/A')}", info_style),
         Paragraph(f"<b>Time Limit:</b> {quiz['time_limit']} minutes", info_style)],
        [Paragraph(f"<b>Questions:</b> {quiz['question_count']}", info_style),
         Paragraph(f"<b>Submissions:</b> {len(results)}", info_style)]
    ]
    info_table = Table(info_data, colWidths=[document.width * 0.7, document.width * 0.3])
    info_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4)
    ]))
    content.append(info_table)
    content.append(Spacer(1, 12))
    content.append(Paragraph('<i>Ranking: Score descending, then time taken ascending. All timestamps are UTC.</i>', cell))
    content.append(Spacer(1, 8))
    
    # TABLE
    headings = ['Rank', 'Student', 'Roll no.', 'Score', '%']
    if quiz['pass_fail_enabled']:
        headings.append('Status')
    headings.extend(['Correct', 'Wrong', 'Blank', 'Time (s)', 'Submitted (UTC)'])
    
    data = [[paragraph(value, header_cell) for value in headings]]
    for rank, result in enumerate(results, 1):
        submitted = datetime.fromtimestamp(result['submitted_at'], timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        values = [rank, result['name'], result['roll_number'], f"{result['score']}/{result['total']}",
                  f"{result['percentage']:.2f}"]
        if quiz['pass_fail_enabled']:
            status = 'PASS' if result['percentage'] >= quiz['pass_percentage'] else 'FAIL'
            values.append(status)
        values.extend([result['correct'], result['wrong'], result['unanswered'],
                  result['time_taken'], submitted])
        data.append([paragraph(value) for value in values])
        
    if not results:
        content.append(paragraph('No submitted attempts yet.'))
        
    colWidths = [32, 135, 95, 40, 40, 45, 45, 45, 45, 60, 118] if quiz['pass_fail_enabled'] else [32, 145, 95, 45, 45, 45, 45, 45, 60, 133]
    table = Table(data, colWidths=colWidths, repeatRows=1, hAlign='LEFT')
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#123453')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor('#f9fafb'), colors.white]),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8), ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('LINEBELOW', (0, 0), (-1, 0), 2, colors.HexColor('#18a999'))
    ]))
    content.append(table)
    
    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont('Helvetica', 8)
        canvas.setFillColor(colors.HexColor('#52637c'))
        
        # Left footer
        canvas.drawString(28, 18, 'College to Campus · Hindusthan College of Technology, Salem – Tamil Nadu')
        
        # Right footer
        canvas.drawRightString(813, 18, f'Page {doc.page}')
        
        # Center footer
        canvas.drawCentredString(841 / 2, 18, 'Designed & Developed by LOGAMITHRAN BS')
        canvas.restoreState()
        
    document.build(content, onFirstPage=footer, onLaterPages=footer)
    stream.seek(0)
    return stream
