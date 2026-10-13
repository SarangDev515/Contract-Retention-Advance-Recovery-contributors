"""Small dependency-free OOXML writer, using only Python's standard library.

Strings are inline strings (never formulas); totals are computed source-data values.
"""
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED
from xml.sax.saxutils import escape


def _column(index):
    result = ''
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def workbook(sheets):
    output = BytesIO()
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    rel = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    formats = ''.join('<numFmt numFmtId="%d" formatCode="%s"/>' % (164 + digits, '#,##0' + ('.' + '0' * digits if digits else '')) for digits in range(7))
    number_styles = ''.join('<xf numFmtId="%d" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>' % (164 + digits) for digits in range(7))
    bold_number_styles = ''.join('<xf numFmtId="%d" fontId="1" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyFont="1"/>' % (164 + digits) for digits in range(7))
    styles = '<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="' + ns + '">' +         '<numFmts count="7">' + formats + '</numFmts>' + \
        '<fonts count="3"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>' +         '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>' +         '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>' +         '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>' +         '<cellXfs count="18"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>' +         '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0"/>' +         '<xf numFmtId="4" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>' + number_styles + bold_number_styles + '<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'
    with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
        overrides = ['<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
                     '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
        sheet_defs, relations = [], []
        for idx, (name, rows) in enumerate(sheets, 1):
            sheet_defs.append('<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (escape(name, {'"': '&quot;'}), idx, idx))
            relations.append('<Relationship Id="rId%d" Type="%s/worksheet" Target="worksheets/sheet%d.xml"/>' % (idx, rel, idx))
            overrides.append('<Override PartName="/xl/worksheets/sheet%d.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % idx)
            cells = []
            customer_heading_rows = []
            for row_idx, row in enumerate(rows, 1):
                customer_heading = bool(row and isinstance(row[0], dict) and 'customer_heading' in row[0])
                if customer_heading:
                    customer_heading_rows.append(row_idx)
                    row = [row[0]['customer_heading']]
                content = []
                subtotal = bool(row and row[0] == 'Subtotal')
                for col_idx, value in enumerate(row, 1):
                    ref = _column(col_idx) + str(row_idx)
                    style = 2
                    if isinstance(value, tuple) and len(value) == 2:
                        value, digits = value
                        style = 3 + max(0, min(6, int(digits)))
                    if subtotal:
                        style = 10 + (style - 3 if style >= 3 else 2)
                    if isinstance(value, (int, float)):
                        content.append('<c r="%s" s="%d"><v>%s</v></c>' % (ref, style, value))
                    else:
                        text = ''.join(c for c in str(value or '') if ord(c) >= 32 or c in '\t\n\r')
                        content.append('<c r="%s" s="%d" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>' %
                                       (ref, 17 if customer_heading else (1 if subtotal or row_idx in (1, 4) else 0), escape(text)))
                cells.append('<row r="%d">%s</row>' % (row_idx, ''.join(content)))
            width = max((len(row) for row in rows), default=1)
            sheet = '<worksheet xmlns="%s"><sheetViews><sheetView workbookViewId="0"><pane ySplit="4" topLeftCell="A5" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols><col min="1" max="%d" width="23" customWidth="1"/></cols><sheetData>%s</sheetData></worksheet>' % (ns, width, ''.join(cells))
            if customer_heading_rows:
                merges = ''.join('<mergeCell ref="A%d:%s%d"/>' % (row, _column(width), row) for row in customer_heading_rows)
                sheet = sheet.replace('</sheetData>', '</sheetData><mergeCells count="%d">%s</mergeCells>' % (len(customer_heading_rows), merges))
            archive.writestr('xl/worksheets/sheet%d.xml' % idx, sheet)
        relations.append('<Relationship Id="rIdStyles" Type="%s/styles" Target="styles.xml"/>' % rel)
        archive.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>%s</Types>' % ''.join(overrides))
        archive.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="%s/officeDocument" Target="xl/workbook.xml"/></Relationships>' % rel)
        archive.writestr('xl/workbook.xml', '<workbook xmlns="%s" xmlns:r="%s"><sheets>%s</sheets></workbook>' % (ns, rel, ''.join(sheet_defs)))
        archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">%s</Relationships>' % ''.join(relations))
        archive.writestr('xl/styles.xml', styles)
    return output.getvalue()
