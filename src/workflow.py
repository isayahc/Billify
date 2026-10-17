"""The Billify expense workflow: read invoices, clean them up, run the AP controls, fill the expense portal.

Agent v1 fills the portal with the values as written on the invoice; agent v2 fills it with the normalized
values Billify already computed. Reading, cleanup and controls are identical, so both versions can be
replayed on the same inputs and compared.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from contextlib import nullcontext
from datetime import date
from decimal import Decimal
from pathlib import Path, PurePath
from typing import Any, Callable
from uuid import uuid4

from src.extract import ExtractionError, match_vendor, read_pdf, to_csv
from src.invoices import WorkflowTrace, process_csv
from src.portal import CURRENCIES, FIELDS, Portal, PortalRejected, validate

VERSIONS = ('v1', 'v2')
MAX_FILES = 60
MAX_TEXT = 100_000
LABELS = {'vendor': 'Vendor', 'invoice_number': 'Invoice number', 'invoice_date': 'Invoice date', 'amount': 'Amount',
          'currency': 'Currency', 'category': 'Category', 'cost_center': 'Cost center', 'description': 'Description'}
MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']
MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October',
               'November', 'December']
CURRENCY_CODES = {'$': 'USD', 'US$': 'USD', 'USD': 'USD', '€': 'EUR', 'EUR': 'EUR', '£': 'GBP', 'GBP': 'GBP',
                  'C$': 'CAD', 'CA$': 'CAD', 'CAD': 'CAD'}
# Line-item wording -> (the reader's label, the portal's category). First match wins.
ITEM_RULES = [
    (r'cater|lunch|meal', 'Catering', 'Meals'),
    (r'standing desk|desk frame|chair|monitor|laptop|equipment', 'Furniture', 'Equipment'),
    (r'copy paper|paper|toner|brochure|carton|stationery|supplies', 'Supplies', 'Office Supplies'),
    (r'security audit|legal|consult|design sprint|staffing|advisory|audit', 'Consulting', 'Professional Services'),
    (r'storage|endpoint|compute|cloud|support plan|licen[sc]e|saas|subscription|hosting|seat', 'Cloud and IT', 'Software'),
    (r'hvac|cleaning|janitor|maintenance visit|facilit', 'Building services', 'Facilities'),
    (r'courier|shipping|freight|delivery|trucking', 'Courier', 'Shipping'),
    (r'airfare|flight|hotel|travel', 'Airfare', 'Travel'),
    (r'forklift|repair|tire|wheel', 'Equipment repair', 'Equipment'),
    (r'pallets?\b', 'Pallets', 'Equipment'),
]
MONEY = r'(?:CA\$|US\$|[$€£])?\s?(?:\d{1,3}(?:[.,]\d{3})*[.,]\d{2}|\d+[.,]\d{2})(?!\d)(?:\s?[€£])?'
DATE_TOKEN = re.compile(r'(\d{4}-\d{2}-\d{2}|\d{1,2}\.\d{1,2}\.\d{4}|\d{1,2}/\d{1,2}/\d{2,4}'
                        r'|[A-Z][a-z]{2,8}\.?\s*\d{1,2},?\s*\d{4}|\d{1,2} [A-Z][a-z]{2,8}\.?,? \d{4})')
NON_US = re.compile(r'United Kingdom|England|Scotland|Wales|Ireland|Germany|Deutschland|France|Canada|Netherlands'
                    r'|Belgium|Spain|Italy|Austria|Switzerland', re.I)
US_STATE_ZIP = re.compile(r'\b[A-Z]{2} \d{5}(?:-\d{4})?\b')
BANK_CHANGE = re.compile(r'new remittance|new bank (?:details|account)|updated? (?:bank|remittance|payment) details'
                         r'|payments to the old account', re.I)


# ---------- reading ----------

def money_all(line: str) -> list[str]:
    """Every money-looking token in a line, in order."""
    return [m.group(0).strip() for m in re.finditer(MONEY, line)]


def money_split(token: str) -> tuple[str, str]:
    """Split "CA$3,066.00" or "8.315,00 €" into its symbol and number as written."""
    m = re.match(r'^(CA\$|US\$|[$€£])?\s?(.*?)\s?([€£])?$', token)
    return (m.group(1) or m.group(3) or ''), m.group(2)


def clean_amount(written: str) -> str:
    """Turn 1,250.00, 1.250,00 or $1,250.00 into 1250.00; empty when it is not clearly a number."""
    compact = re.sub(r'[^\d.,]', '', written or '')
    if m := re.fullmatch(r'(\d{1,3}(?:[.,]\d{3})*|\d+)[.,](\d{1,2})', compact):
        number = re.sub(r'[.,]', '', m.group(1)) + '.' + m.group(2)
    elif re.fullmatch(r'\d{1,3}(?:[.,]\d{3})+|\d+', compact):
        number = re.sub(r'[.,]', '', compact)
    else:
        return ''
    return format(Decimal(number), '.2f')


def vendor_region(text: str) -> str:
    """'us', 'non-us' or '' from the vendor's own address block (before "Bill to")."""
    block = re.split(r'\bBill\s*to\b', text, maxsplit=1, flags=re.I)[0]
    if NON_US.search(block):
        return 'non-us'
    if US_STATE_ZIP.search(block):
        return 'us'
    return ''


def parse_date(written: str, region: str) -> tuple[str, str]:
    """Return (YYYY-MM-DD or '', note). Never guesses: a day/month order the page can't settle is left empty."""
    s = (written or '').strip()
    if not s:
        return '', ''
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', s):
        return checked(int(s[:4]), int(s[5:7]), int(s[8:]), '')
    if m := re.fullmatch(r'(\d{1,2})\.(\d{1,2})\.(\d{4})', s):
        return checked(int(m.group(3)), int(m.group(2)), int(m.group(1)), 'day.month.year')
    if m := re.fullmatch(r'(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})', s):
        a, b, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        year = 2000 + year if year < 100 else year
        if a > 12 and b <= 12:
            return checked(year, b, a, f'{a} cannot be a month, so day first')
        if b > 12 and a <= 12:
            return checked(year, a, b, f'{b} cannot be a month, so month first')
        if a == b:
            return checked(year, a, b, '')
        if a > 12 and b > 12:
            return '', f'{s} is not a valid date'
        if region == 'us':
            return checked(year, a, b, 'US vendor, read month first')
        first = f'{a} {MONTH_NAMES[b - 1]}'
        second = f'{b} {MONTH_NAMES[a - 1]}'
        where = 'non-US vendor' if region == 'non-us' else 'vendor country unknown'
        return '', f'{s} could be {first} or {second} {year} ({where}); not guessed'
    if m := re.fullmatch(r'([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s*(\d{4})', s):
        if m.group(1)[:3].lower() in MONTHS:
            return checked(int(m.group(3)), MONTHS.index(m.group(1)[:3].lower()) + 1, int(m.group(2)), '')
    if m := re.fullmatch(r'(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})', s):
        if m.group(2)[:3].lower() in MONTHS:
            return checked(int(m.group(3)), MONTHS.index(m.group(2)[:3].lower()) + 1, int(m.group(1)), '')
    return '', f'could not read "{s}" as a date'


def checked(year: int, month: int, day: int, note: str) -> tuple[str, str]:
    """Build an ISO date, refusing impossible calendar days."""
    try:
        return date(year, month, day).isoformat(), note
    except ValueError:
        return '', f'{year}-{month:02d}-{day:02d} is not a calendar date'


def find_date(text: str, labels: str, skip_due: bool) -> str:
    """The first date written after one of the labels, as written."""
    for m in re.finditer(r'(?:^|[^A-Za-z])(' + labels + r')\s*:\s*([^\n]*)', text, re.I):
        start = m.start(1)
        if skip_due and re.search(r'due\s*$', text[max(0, start - 5):start], re.I):
            continue
        token = DATE_TOKEN.search(m.group(2))
        if token:
            return re.sub(r',(\d)', r', \1', re.sub(r'([A-Za-z])(\d)', r'\1 \2', token.group(1), count=1))
    return ''


def extract_text(text: str) -> dict[str, Any]:
    """Read invoice text into fields exactly as written (port of the demo's reader v2, plus PO and due date)."""
    text = re.sub(r'\bl(nvo|nvor)', r'I\1', text)
    text = re.sub(r'\bInvorce\b', 'Invoice', text, flags=re.I)
    lines = [re.sub(r'\s+', ' ', l).strip() for l in text.splitlines()]
    lines = [l for l in lines if l]

    def first(pattern: str, flags: int = re.I) -> str:
        m = re.search(pattern, text, flags)
        return m.group(1).strip() if m else ''

    candidates = [re.sub(r'\s+invoice\s*$', '', re.sub(r'^invoice\b[\s:]*', '', l, flags=re.I), flags=re.I).strip()
                  for l in lines]
    vendor = next((l for l in candidates if l and re.search(r'[A-Za-z]{3}', l) and not re.match(
        r'(from|to|subject|date|message-id|bill to|ship to|page \d|po box|duplicate|voice|fax)', l, re.I)), '')
    number = first(r'(?:Invoice\s*(?:No\.?|Number|Num|#)|Rechnungsnr\.?\s*/\s*Invoice No\.?'
                   r'|Facture N°\s*/\s*Invoice No\.?)\s*[:.#\'"`]*\s*([^\n]*)')
    number = re.sub(r'^[:\s]+', '', number).strip()

    total_token = ''
    for m in re.finditer(r'^.*\b(?:TOTAL DUE|Amount Due|Balance Due|Grand Total|Total Invoice Amount'
                         r'|Gesamt\s*/\s*Total|Total)\b.*$', text, re.I | re.M):
        tokens = money_all(re.sub(r'\s+', ' ', m.group(0)))
        if tokens:
            total_token = tokens[-1]
    if not total_token:
        best = Decimal('0')
        for token in money_all(text):
            value = Decimal(clean_amount(money_split(token)[1]) or '0')
            if value > best:
                best, total_token = value, token
    symbol, amount = money_split(total_token) if total_token else ('', '')
    currency = first(r'Currency:\s*([A-Z]{3})\b', 0) or symbol or first(r'\b(USD|EUR|GBP|CAD)\b', 0)
    if not currency and '$' in text:
        currency = '$'

    item_re = re.compile(r'^(.+?)\s+(\d{1,4})\s+((?:CA\$|US\$|[$€£])?\d[\d.,]*(?:\s?[€£])?)'
                         r'\s+((?:CA\$|US\$|[$€£])?\d[\d.,]*(?:\s?[€£])?)$')
    items, tax, subtotal = [], None, ''
    for line in lines:
        if re.match(r'subtotal', line, re.I):
            tokens = money_all(line)
            subtotal = money_split(tokens[-1])[1] if tokens else ''
            continue
        if re.match(r'(description|total|payment|please)', line, re.I):
            continue
        m = item_re.match(line)
        if m and not re.search(r'\btax\b|\bVAT\b', m.group(1), re.I):
            items.append({'desc': m.group(1).strip(), 'qty': int(m.group(2)),
                          'amount': Decimal(clean_amount(money_split(m.group(4))[1]) or '0')})
        elif re.search(r'\b(tax|VAT|USt|TVA|MwSt)\b', line, re.I):
            tokens = money_all(line)
            if tokens:
                tax = (tax or Decimal('0')) + Decimal(clean_amount(money_split(tokens[-1])[1]) or '0')

    warnings = []
    total = Decimal(clean_amount(amount) or '0')
    if items:
        line_sum = sum(i['amount'] for i in items)
        if abs(line_sum + (tax or 0) - total) > Decimal('0.02'):
            warnings.append(f'Line items ({line_sum:.2f}) plus tax ({(tax or 0):.2f}) do not equal the total ({total:.2f})')

    description = first(r'^\s*Description\s*:\s*(.+)$', re.I | re.M)
    label, category = '', ''
    if items:
        by_category: dict[str, list[Any]] = {}
        for item in items:
            rule = next((r for r in ITEM_RULES if re.search(r[0], item['desc'], re.I)), None)
            if rule:
                by_category.setdefault(rule[2], [Decimal('0'), rule])[0] += item['amount']
        if by_category:
            category = max(by_category, key=lambda k: by_category[k][0])
            label = by_category[category][1][1]
        if not description:
            top = max(items, key=lambda i: i['amount'])
            description = top['desc'] + (f' (+{len(items) - 1} more)' if len(items) > 1 else '')
    else:
        rule = next((r for r in ITEM_RULES if re.search(r[0], description or text, re.I)), None)
        if rule:
            label, category = rule[1], rule[2]

    invoice_date = find_date(text, r'Invoice\s+Date|Datum\s*/\s*Date|Date', True)
    if not invoice_date:
        unlabeled = re.search(r'(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}|[A-Z][a-z]{2,8}\.?\s*\d{1,2},?\s*\d{4})', text)
        invoice_date = unlabeled.group(1) if unlabeled else ''
    return {
        'vendor': vendor, 'invoice_number': number, 'invoice_date': invoice_date,
        'due_date': find_date(text, r'Due\s*Date|F[äa]llig\s*/\s*Due|Due', False),
        'amount': amount, 'currency': currency, 'subtotal': subtotal,
        'tax': format(tax, '.2f') if tax is not None else '',
        # A PO number has a digit, so "PO Box 12" is not one; the colon after the label is optional.
        'po_number': first(r'(?:Purchase\s+Order(?:\s+Number)?|Your\s+PO\s+Ref|PO\s+(?:Number|No\.?|#)|\bPO)'
                           r'\s*(?::\s*|\s)([A-Za-z0-9-]*\d[A-Za-z0-9-]*)'),
        'cost_center': first(r'Cost\s*Cent(?:er|re)[\s:]*([A-Za-z0-9-]+)'),
        'description': description, 'category_label': label, 'category': category,
        'region': vendor_region(text), 'bank_change': bool(BANK_CHANGE.search(text)),
        'items': len(items), 'warnings': warnings,
    }


def canonical_invoice_number(written: str) -> str:
    """INV 563, inv-902, No. 0564, Invoice #881 -> INV-0563, INV-0902, INV-0564, INV-0881."""
    s = re.sub(r'^[^A-Z0-9#]+', '', (written or '').upper().strip())
    for _ in range(2):
        s = re.sub(r'^(INVOICE|NO\.?|N°|#)\s*', '', s)
    s = s.lstrip('#').strip()
    m = re.fullmatch(r'([A-Z]{1,4})?[\s-]*0*(\d{1,10})', s)
    if not m:
        return re.sub(r'[^A-Z0-9-]', '-', s)
    return f'{m.group(1) or "INV"}-{m.group(2).zfill(4)}'


# ---------- cleanup ----------

def approved_vendor(written: str, reference: dict[str, Any]) -> str:
    """The approved vendor name when the written name is that vendor or one of its approved aliases."""
    name = (written or '').strip().casefold()
    for vendor in reference['vendors']:
        if name and name in {n.casefold() for n in [vendor['name'], *vendor.get('aliases', [])]}:
            return vendor['name']
    return ''


def written_fields(raw: dict[str, Any]) -> dict[str, str]:
    """What v1 types into the portal: every value exactly as the invoice wrote it."""
    return {'vendor': raw['vendor'], 'invoice_number': raw['invoice_number'], 'invoice_date': raw['invoice_date'],
            'amount': raw['amount'], 'currency': raw['currency'], 'category': raw['category_label'],
            'cost_center': raw['cost_center'], 'description': raw['description'][:200]}


def note(field: str, written: str, sent: str, why: str = '') -> dict[str, str]:
    """One cleanup the reviewer should see: what the invoice said and what Billify will send."""
    return {'field': field, 'label': LABELS.get(field, field), 'written': written, 'sent': sent, 'why': why}


# ---------- the run ----------

def traced(handler: Any, session_id: str, name: str) -> tuple[Any, WorkflowTrace]:
    """A PRISM session context and a root run for one invoice."""
    if handler:
        from prismtrace import session
        return session(session_id), WorkflowTrace(handler, name)
    return nullcontext(), WorkflowTrace(None, name)


def decode_files(payload: Any) -> tuple[str, str, list[dict[str, Any]]]:
    """Validate a read request: version, default cost center and up to 60 files of text or PDF."""
    if not isinstance(payload, dict):
        raise ValueError('Expected a JSON object.')
    version = payload.get('version', 'v2')
    if version not in VERSIONS:
        raise ValueError('version must be v1 or v2.')
    default_cc = str(payload.get('default_cost_center') or '').strip()
    if default_cc and not re.fullmatch(r'CC-\d{3}', default_cc):
        raise ValueError('default_cost_center must look like CC-210.')
    files = payload.get('files')
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise ValueError(f'Provide 1 to {MAX_FILES} files.')
    out = []
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not item['name'].strip():
            raise ValueError('Each file needs a name.')
        name = PurePath(item['name']).name[:120]
        text, pdf = item.get('text'), item.get('pdf')
        if text is not None and (not isinstance(text, str) or len(text) > MAX_TEXT):
            raise ValueError(f'{name}: text must be a string of at most {MAX_TEXT} characters.')
        data = None
        if pdf is not None:
            try:
                data = base64.b64decode(str(pdf), validate=True)
            except binascii.Error as exc:
                raise ValueError(f'{name} is not valid base64.') from exc
            if not data.startswith(b'%PDF-'):
                raise ValueError(f'{name} is not a PDF.')
        out.append({'name': name, 'source': str(item.get('source') or 'Upload')[:20], 'text': text, 'pdf': data})
    return version, default_cc, out


def raw_from_claude(fields: dict[str, Any]) -> dict[str, Any]:
    """Shape a Claude PDF read (extract.read_pdf) like a text read; as-written values stay as written."""
    written = fields.get('as_written') or {}
    return {'vendor': fields.get('vendor_name', ''), 'invoice_number': fields.get('invoice_number', ''),
            'invoice_date': written.get('invoice_date') or fields.get('invoice_date', ''),
            'due_date': written.get('due_date') or fields.get('due_date', ''),
            'amount': written.get('total') or fields.get('total', ''),
            'currency': written.get('currency') or fields.get('currency', ''), 'currency_code': fields.get('currency', ''),
            'subtotal': fields.get('subtotal', ''), 'tax': fields.get('tax', ''), 'po_number': fields.get('po_number', ''),
            'cost_center': '', 'description': '', 'category_label': '', 'category': '', 'region': '',
            'bank_change': False, 'items': 0, 'warnings': list(fields.get('ambiguities') or [])}


def read_items(payload: Any, reference: dict[str, Any], portal: Portal, handler: Any = None,
               read_pdf_fn: Callable[[bytes, str], dict[str, Any]] | None = None,
               today: date | None = None) -> dict[str, Any]:
    """Read, clean up and check every file; nothing is sent to the portal here."""
    version, default_cc, files = decode_files(payload)
    today = today or date.today()
    items = [read_one(file, version, default_cc, reference, handler, read_pdf_fn or read_pdf, today) for file in files]
    for item in items:
        if item['status'] == 'read':
            portal.remember_read(item['id'], version, item['name'], item['clean'])
    run_controls([i for i in items if i['status'] == 'read'], reference, handler)
    return {'version': version, 'items': items, 'tracing_enabled': handler is not None}


def read_one(file: dict[str, Any], version: str, default_cc: str, reference: dict[str, Any], handler: Any,
             read_pdf_fn: Callable[[bytes, str], dict[str, Any]], today: date) -> dict[str, Any]:
    """One invoice through reading and cleanup, as eight traced steps in its own PRISM session."""
    item: dict[str, Any] = {'id': str(uuid4()), 'name': file['name'], 'source': file['source'], 'version': version,
                            'status': 'read', 'why': '', 'steps': []}
    context, trace = traced(handler, item['id'], 'read_invoice')

    def step(name: str, operation: Callable[[], Any], describe: Callable[[Any], tuple[str, str]]) -> Any:
        """Run a traced tool step and keep a readable line for the agent-steps drawer."""
        try:
            result = trace.step(name, operation)
        except Exception as exc:
            item['steps'].append({'run': 'read_invoice', 'name': name, 'status': 'error', 'output': str(exc)})
            raise
        status, output = describe(result)
        item['steps'].append({'run': 'read_invoice', 'name': name, 'status': status, 'output': output})
        return result

    with context, trace:
        raw = read_raw(file, item, step, read_pdf_fn)
        if raw is None:
            return item
        written = written_fields(raw)
        clean, notes = dict(written), []

        def dates() -> dict[str, str]:
            """Read the invoice and due dates as YYYY-MM-DD, with the reason for any reading."""
            iso, why = parse_date(raw['invoice_date'], raw['region'])
            due, due_why = parse_date(raw['due_date'], raw['region'])
            return {'iso': iso, 'why': why, 'due': due, 'due_why': due_why}

        day = step('normalize_date', dates, lambda r: ('ok' if r['iso'] else 'warn', (
            f"{raw['invoice_date']} → {r['iso']}" + (f" ({r['why']})" if r['why'] else '')) if r['iso']
            else (r['why'] or 'No invoice date found')))
        clean['invoice_date'] = day['iso']
        if raw['invoice_date'] != day['iso']:
            notes.append(note('invoice_date', raw['invoice_date'], day['iso'], day['why']))

        amount = step('normalize_amount', lambda: clean_amount(raw['amount']), lambda r: (
            'ok' if r else 'warn', (f"{raw['amount']} → {r}" if r != raw['amount'] else r) if r else 'No total found'))
        clean['amount'] = amount
        if raw['amount'] != amount:
            notes.append(note('amount', raw['amount'], amount))

        def currency() -> str:
            """The ISO code for the written symbol or code; an unknown code is kept so the portal can refuse it."""
            if not raw['currency']:
                return 'USD'
            return CURRENCY_CODES.get(raw['currency']) or raw.get('currency_code') or raw['currency']

        code = step('normalize_currency', currency, lambda r: (
            ('ok' if r in CURRENCIES else 'warn', f"{raw['currency']} → {r}" if raw['currency'] != r else r)
            if raw['currency'] else ('warn', 'No currency on the invoice; USD assumed')))
        clean['currency'] = code
        if raw['currency'] != code:
            notes.append(note('currency', raw['currency'], code, '' if raw['currency'] else 'none on the invoice, USD assumed'))

        identity = step('normalize_identity', lambda: {
            'vendor': approved_vendor(raw['vendor'], reference),
            'number': canonical_invoice_number(raw['invoice_number']) if raw['invoice_number'] else ''}, lambda r: (
            'ok' if r['vendor'] and r['number'] else 'warn',
            ('Approved vendor ' + r['vendor'] if r['vendor'] else f"\"{raw['vendor']}\" is not an approved vendor name")
            + (f" · invoice {raw['invoice_number']} → {r['number']}" if r['number'] != raw['invoice_number'] else '')))
        clean['vendor'] = identity['vendor'] or raw['vendor']
        clean['invoice_number'] = identity['number']
        if clean['vendor'] != raw['vendor']:
            notes.append(note('vendor', raw['vendor'], clean['vendor'], 'approved vendor name'))
        if clean['invoice_number'] != raw['invoice_number']:
            notes.append(note('invoice_number', raw['invoice_number'], clean['invoice_number']))

        mapped = step('map_category', lambda: {'category': raw['category'], 'cost_center': raw['cost_center'] or default_cc},
                      lambda r: ('ok' if r['category'] else 'warn', (
                          f"{raw['category_label']} → {r['category']}" if r['category'] else 'No category could be mapped')
                          + ('' if raw['cost_center'] else (f' · default cost center {default_cc}' if default_cc
                                                            else ' · no cost center'))))
        clean['category'] = mapped['category']
        clean['cost_center'] = mapped['cost_center']
        if raw['category_label'] != clean['category'] and clean['category']:
            notes.append(note('category', raw['category_label'], clean['category']))
        if not raw['cost_center'] and default_cc:
            notes.append(note('cost_center', '', default_cc, f'none on the invoice, default {default_cc} applied'))

        fields = written if version == 'v1' else clean
        precheck = step('validate_fields', lambda: validate(fields, today), lambda r: (
            'warn' if r else 'ok', 'Matches the portal rules' if not r else
            f"{len(r)} field{'s' if len(r) != 1 else ''} would be rejected: " + ', '.join(sorted({e['code'] for e in r}))))

    checks = []
    if not day['iso'] and 'not guessed' in day['why']:
        checks.append({'code': 'AMBIGUOUS_DATE', 'field': 'invoice_date',
                       'message': day['why'][0].upper() + day['why'][1:] + '. Confirm the date before sending.'})
    if day['iso'] and day['iso'] > today.isoformat():
        checks.append({'code': 'DATE_IN_FUTURE', 'field': 'invoice_date', 'message': f"Dated {day['iso']}, after today."})
    if raw['bank_change']:
        checks.append({'code': 'BANK_DETAILS_CHANGED', 'field': 'vendor',
                       'message': 'The invoice announces new bank details. Confirm them with the vendor by phone.'})
    item.update(written=written, clean=clean, fields=dict(fields), notes=notes, warnings=raw['warnings'], checks=checks,
                precheck=precheck, po_number=raw['po_number'], due_date=day['due'],
                totals={'subtotal': clean_amount(raw['subtotal']),
                        # No tax line under a subtotal means no tax, so the arithmetic check still runs.
                        'tax': raw['tax'] or ('0.00' if raw['subtotal'] else '')})
    return item


def read_raw(file: dict[str, Any], item: dict[str, Any], step: Callable[..., Any],
             read_pdf_fn: Callable[[bytes, str], dict[str, Any]]) -> dict[str, Any] | None:
    """Get the as-written fields from text, or from Claude for a PDF without text; None when unusable."""
    text = file['text']
    if text is not None and len(re.sub(r'\s', '', text)) >= 30:
        step('read_document', lambda: {'characters': len(text)},
             lambda r: ('ok', f"{file['name']} · {file['source']} · {r['characters']:,} characters of text"))
        raw = step('extract_fields', lambda: extract_text(text), describe_extract)
    elif file['pdf'] is not None:
        try:
            fields = step('read_document', lambda: read_pdf_fn(file['pdf'], file['name']),
                          lambda r: ('ok', f"{file['name']} · read by {r.get('model') or 'Claude'}"))
        except ExtractionError as exc:
            item.update(status='needs_text', why=f'This PDF has no text layer and Claude is not available ({exc}). '
                        'Paste the invoice text instead.')
            return None
        except ValueError as exc:
            item.update(status='needs_text', why=f'{exc} Paste the invoice text instead.')
            return None
        if not fields.get('is_invoice'):
            item.update(status='not_invoice', why='This document is not an invoice.')
            return None
        raw = step('extract_fields', lambda: raw_from_claude(fields), describe_extract)
    else:
        item.update(status='needs_text', why='There is no readable text in this file. Paste the invoice text instead.')
        return None
    if not raw['invoice_number'] and not raw['amount']:
        item.update(status='not_invoice', why='No invoice number or total was found.')
        return None
    return raw


def describe_extract(raw: dict[str, Any]) -> tuple[str, str]:
    """Summarise what the reader found."""
    found = [LABELS[k] for k in ('vendor', 'invoice_number', 'invoice_date', 'amount', 'currency') if raw[k]]
    extra = ['PO ' + raw['po_number']] if raw['po_number'] else ['no PO number']
    if raw['items']:
        extra.append(f"{raw['items']} line item{'s' if raw['items'] != 1 else ''}")
    return ('ok' if len(found) == 5 else 'warn', ', '.join(found + extra))


def run_controls(items: list[dict[str, Any]], reference: dict[str, Any], handler: Any = None) -> None:
    """Run the AP controls invoice by invoice, so an earlier pass counts against later duplicates and POs."""
    adjusted = json.loads(json.dumps(reference))
    for item in items:
        clean = item['clean']
        vendor_id, matched_by = match_vendor({'vendor_name': item['written']['vendor'], 'po_number': item['po_number']},
                                             adjusted)
        row = {'invoice_number': clean['invoice_number'], 'vendor_id': vendor_id, 'vendor_name': item['written']['vendor'],
               'invoice_date': clean['invoice_date'], 'due_date': item['due_date'], 'po_number': item['po_number'],
               'subtotal': item['totals']['subtotal'], 'tax': item['totals']['tax'], 'total': clean['amount'],
               'currency': clean['currency']}
        result = process_csv(to_csv([row]), adjusted, handler, [item['id']])[0]
        controls = result['issues']
        item['controls'] = controls
        item['vendor_id'], item['vendor_matched_by'] = vendor_id, matched_by
        item['hold'] = bool(controls or item['checks'])
        item['steps'].extend(control_steps(controls, row, matched_by))
        if result['status'] == 'passed':
            adjusted['existing_bills'].append({'vendor_id': vendor_id, 'invoice_number': clean['invoice_number']})
            for po in adjusted['purchase_orders']:
                if po['number'] == item['po_number']:
                    po['remaining_amount'] = format(Decimal(str(po['remaining_amount'])) - Decimal(clean['amount']), '.2f')


# The validator's seven traced steps, with the finding codes each one raises (see invoices.process_csv).
CONTROL_STEPS = [('load_invoice', ()),
                 ('normalize_fields', ('MISSING_FIELD', 'MISSING_PO', 'INVALID_AMOUNT', 'INVALID_DATE',
                                       'INVALID_DATE_ORDER', 'INVALID_CURRENCY')),
                 ('lookup_vendor', ('UNKNOWN_VENDOR', 'VENDOR_NAME_MISMATCH')),
                 ('lookup_purchase_order', ('UNKNOWN_PO', 'PO_VENDOR_MISMATCH', 'UNEXPECTED_CURRENCY')),
                 ('search_existing_bills', ('DUPLICATE_INVOICE',)),
                 ('validate_amounts', ('TOTAL_MISMATCH', 'PO_OVERBILLING', 'ZERO_TOTAL')),
                 ('save_decision', ())]
ZERO_TOTAL = 'Invoice total must be greater than zero.'


def control_steps(controls: list[dict[str, str]], row: dict[str, str], matched_by: str) -> list[dict[str, str]]:
    """One readable line per validator step, from the findings that step raised."""
    has_po = bool(row['po_number']) and not any(c['code'] == 'UNKNOWN_PO' for c in controls)
    amounts = 'Subtotal plus tax equals the total' if row['subtotal'] and row['tax'] else 'Total is above zero'
    passed = {'load_invoice': f"Invoice {row['invoice_number'] or '(no number)'}",
              'normalize_fields': 'Required fields present; dates and amounts valid',
              'lookup_vendor': f"Approved vendor {row['vendor_id']}"
                               + (' (found by name)' if matched_by == 'name' else ' (found by PO)'),
              'lookup_purchase_order': f"{row['po_number']} belongs to this vendor, in {row['currency']}"
                                       if row['po_number'] else 'No PO to look up',
              'search_existing_bills': 'Not billed before',
              'validate_amounts': amounts + ('; within the PO balance' if has_po else ''),
              'save_decision': 'Passed: ready to send' if not controls else
                               f"Held for review: {len(controls)} finding{'s' if len(controls) != 1 else ''}"}
    steps = []
    for name, codes in CONTROL_STEPS:
        found = [c for c in controls if ('ZERO_TOTAL' if c['message'] == ZERO_TOTAL else c['code']) in codes]
        steps.append({'run': 'validate_invoice', 'name': name, 'status': 'warn' if found or (
            name == 'save_decision' and controls) else 'ok',
            'output': ' '.join(f"{c['code']}: {c['message']}" for c in found) or passed[name]})
    return steps


def decode_transfer(payload: Any) -> tuple[str, str, list[dict[str, Any]]]:
    """Validate a transfer request: version, optional report and the claims to submit."""
    if not isinstance(payload, dict):
        raise ValueError('Expected a JSON object.')
    version = payload.get('version', 'v2')
    if version not in VERSIONS:
        raise ValueError('version must be v1 or v2.')
    report_id = payload.get('report_id') or ''
    if not isinstance(report_id, str):
        raise ValueError('report_id must be a string.')
    items = payload.get('items')
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_FILES:
        raise ValueError(f'Send 1 to {MAX_FILES} invoices.')
    out = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not isinstance(item.get('fields'), dict):
            raise ValueError('Each invoice needs an id and fields.')
        claim = {key: str(item['fields'].get(key) or '')[:300] for key in FIELDS}
        out.append({'id': item['id'][:64], 'name': str(item.get('name') or '')[:120], 'fields': claim})
    return version, report_id, out


def transfer_items(payload: Any, portal: Portal, handler: Any = None) -> dict[str, Any]:
    """Fill and submit each claim inside its invoice's PRISM session; a rejection is a failed tool call."""
    version, report_id, items = decode_transfer(payload)
    if report_id and not portal.has_report(report_id):
        raise ValueError('Unknown report.')
    report_id = report_id or portal.new_report()
    results = []
    for item in items:
        result = {'id': item['id'], 'name': item['name'], 'fields': item['fields'], 'steps': []}
        context, trace = traced(handler, item['id'], 'transfer_invoice')

        def submit() -> dict[str, Any]:
            """Post the claim; anything but 201 raises so the trace records a failed call."""
            status, body = portal.submit(report_id, item['fields'])
            if status != 201:
                raise PortalRejected(status, body)
            return body

        try:
            with context, trace:
                trace.step('fill_portal_form', lambda: dict(item['fields']))
                result['steps'].append({'run': 'transfer_invoice', 'name': 'fill_portal_form', 'status': 'ok',
                                        'output': f'{len(FIELDS)} fields filled on report {report_id} (agent {version})'})
                expense = trace.step('submit_expense', submit)
            result.update(status='created', http=201, expense=expense, errors=[])
            result['steps'].append({'run': 'transfer_invoice', 'name': 'submit_expense', 'status': 'ok',
                                    'output': f"201 · {expense['id']} · pending approval"})
        except PortalRejected as exc:
            result.update(status='rejected', http=exc.status, expense=None, errors=exc.body.get('errors', []))
            result['steps'].append({'run': 'transfer_invoice', 'name': 'submit_expense', 'status': 'error',
                                    'output': str(exc)})
        portal.record_run(item['id'], report_id, item['name'], version, result['status'],
                          [e['code'] for e in result['errors']])
        results.append(result)
    return {'version': version, 'report': portal.report(report_id), 'results': results,
            'summary': {'sent': len(results), 'created': sum(r['status'] == 'created' for r in results),
                        'rejected': sum(r['status'] == 'rejected' for r in results)},
            'alert': portal.alert(), 'causes': causes(results), 'root_cause': root_cause(version, results, portal),
            'tracing_enabled': handler is not None}


CAUSE_LABELS = {'INVALID_DATE_FORMAT': "Date isn't YYYY-MM-DD", 'INVALID_AMOUNT': 'Amount has symbols, commas or a decimal comma',
                'UNSUPPORTED_CURRENCY': "Currency isn't a supported code", 'INVALID_CATEGORY': "Category isn't in the portal's list",
                'REQUIRED': 'A required field is empty', 'INVALID_FORMAT': 'A field has the wrong format',
                'DATE_IN_FUTURE': 'Date is after today', 'DUPLICATE_INVOICE': 'Already on this report'}


def causes(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Failed runs grouped by the error code the portal returned (a run can have several)."""
    groups: dict[str, list[str]] = {}
    for result in results:
        for code in dict.fromkeys(e['code'] for e in result['errors']):
            groups.setdefault(code, []).append(result['name'])
    return sorted(({'code': code, 'label': CAUSE_LABELS.get(code, code), 'runs': len(names), 'invoices': names}
                   for code, names in groups.items()), key=lambda c: -c['runs'])


def root_cause(version: str, results: list[dict[str, Any]], portal: Portal) -> dict[str, Any] | None:
    """For v1 failures, check whether the normalized values Billify had already computed would have passed."""
    failed = [r for r in results if r['status'] == 'rejected']
    if not failed or version != 'v1':
        return None
    known = [(r, portal.read_clean(r['id'])) for r in failed]
    known = [(r, clean) for r, clean in known if clean]
    passing = sum(not validate(clean) for _, clean in known)
    return {'explanation': 'Agent v1 fills the portal with the values as written on the invoice. The normalized values '
                           'were computed during reading but not used. Agent v2 sends the normalized values.',
            'failed': len(failed), 'checked': len(known), 'would_pass': passing}


def samples(root: Path) -> dict[str, Any]:
    """The demo folder and inbox, made of the sample invoice texts."""
    folder = sorted((root / 'data/sample_invoices').glob('*.txt'))
    texts = {p.name: p.read_text(encoding='utf-8') for p in folder}
    inbox = [('invoice_001.txt', 'Billing, Acme Corporation', 'Invoice INV-0516, catering'),
             ('invoice_002.txt', 'Accounts, Thamesway Consulting', 'Your invoice INV-0696'),
             ('invoice_008.txt', 'Billing, Pixelforge Design', 'Invoice INV-0563 attached'),
             ('invoice_018.txt', 'Billing, Pixelforge Design', 'Reminder: invoice 563 (second notice)'),
             ('invoice_045.txt', 'Billing, Bluepeak Cloud', 'Invoice INV-0090 for October')]
    return {'folder': [{'name': n, 'meta': 'Sample invoice text', 'text': t} for n, t in texts.items()]
                      + [{'name': 'Q3_budget_notes.xlsx', 'meta': '58 KB', 'off': 'Not an invoice'}],
            'inbox': [{'name': n, 'sender': s, 'subject': sub, 'text': texts[n]} for n, s, sub in inbox if n in texts]
                     + [{'name': 'No attachment', 'sender': 'Team Events', 'subject': 'Friday lunch order', 'off': 'No invoice found'},
                        {'name': 'newsletter.html', 'sender': 'Payments Weekly', 'subject': 'October product update',
                         'off': 'No invoice found'}]}
