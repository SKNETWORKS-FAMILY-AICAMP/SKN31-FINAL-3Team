"""Readable, reversible spec/other sections in ERPNext's single terms field."""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser

from .quotation_models import Quotation


def render_separated_terms(quotation: Quotation) -> str:
    parts = ['<p><strong>[규격사항]</strong></p>']
    for index, item in enumerate(quotation.items, 1):
        identity = ' / '.join(filter(None, [item.item_code, item.item_name]))
        parts.append(f'<p><strong>[품목 {index} 규격] {html.escape(identity)}</strong></p>')
        specs = dict(item.specifications)
        if not specs and (item.description or item.raw_description):
            specs['규격 설명'] = item.description or item.raw_description
        parts.append('<table><tbody>')
        for key, value in specs.items():
            if value is None or not str(value).strip():
                continue
            escaped_value = html.escape(str(value), quote=True).replace('\n', '<br>')
            parts.append(f'<tr><td>{html.escape(str(key), quote=True)}</td><td>{escaped_value}</td></tr>')
        parts.append('</tbody></table>')
    parts.append('<p><strong>[그 외 사항]</strong></p>')
    if quotation.notes:
        notes = html.escape(quotation.notes, quote=True).replace('\n', '<br>')
        parts.append(f'<p>{notes}</p>')
    return ''.join(parts)


class _SeparatedTermsParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.section = None
        self.item_index = None
        self.specs: dict[int, dict[str, str]] = {}
        self.notes: list[str] = []
        self.paragraph = None
        self.cell = None
        self.row: list[str] = []
        self.has_specs = False
        self.has_other = False

    def handle_starttag(self, tag, attrs):
        if tag in {'p', 'h2', 'h3', 'h4'}:
            self.paragraph = []
        elif tag == 'tr':
            self.row = []
        elif tag in {'td', 'th'}:
            self.cell = []
        elif tag == 'br':
            if self.cell is not None:
                self.cell.append('\n')
            elif self.paragraph is not None:
                self.paragraph.append('\n')

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)
        elif self.paragraph is not None:
            self.paragraph.append(data)

    def handle_endtag(self, tag):
        if tag in {'td', 'th'} and self.cell is not None:
            self.row.append(''.join(self.cell).strip())
            self.cell = None
        elif tag == 'tr' and self.section == 'specs' and self.item_index is not None:
            if len(self.row) == 2 and self.row[0]:
                self.specs[self.item_index][self.row[0]] = self.row[1]
        elif tag in {'p', 'h2', 'h3', 'h4'} and self.paragraph is not None:
            text = ''.join(self.paragraph).strip()
            self.paragraph = None
            if text == '[규격사항]' and not self.has_other:
                self.section = 'specs'
                self.has_specs = True
            elif text == '[그 외 사항]' and self.has_specs:
                self.section = 'other'
                self.has_other = True
            elif self.section == 'specs':
                match = re.match(r'^\[품목 (\d+) 규격\]', text)
                if match:
                    self.item_index = int(match.group(1))
                    self.specs.setdefault(self.item_index, {})
            elif self.section == 'other' and text:
                self.notes.append(text)


def parse_separated_terms(value: str | None) -> tuple[dict[int, dict[str, str]], str | None] | None:
    parser = _SeparatedTermsParser()
    parser.feed(str(value or ''))
    parser.close()
    if not (parser.has_specs and parser.has_other):
        return None  # Existing mixed terms / portal notes retain their old path.
    return parser.specs, '\n'.join(parser.notes) or None
