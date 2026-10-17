"""Simulated Acme Expense portal: the expense system Billify fills in, with the rules a real one enforces.

It keeps reports, expenses and a log of every transfer run in the same SQLite file as InvoiceStore.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

FIELDS = ['vendor', 'invoice_number', 'invoice_date', 'amount', 'currency', 'category', 'cost_center', 'description']
CATEGORIES = ['Software', 'Travel', 'Meals', 'Office Supplies', 'Equipment', 'Professional Services',
              'Utilities', 'Facilities', 'Shipping']
CURRENCIES = ['USD', 'EUR', 'GBP', 'CAD']
ALERT_WINDOW = 10
ALERT_THRESHOLD = 0.2


def validate(claim: dict[str, Any], today: date | None = None) -> list[dict[str, str]]:
    """Return the portal's field errors for one expense claim (empty when it would be accepted)."""
    errors: list[dict[str, str]] = []

    def add(field: str, code: str, message: str) -> None:
        errors.append({'field': field, 'code': code, 'message': message})

    def text(key: str) -> str:
        value = claim.get(key)
        return '' if value is None else str(value).strip()

    vendor = text('vendor')
    if not vendor:
        add('vendor', 'REQUIRED', 'Vendor is required.')
    elif not 2 <= len(vendor) <= 80:
        add('vendor', 'INVALID_FORMAT', 'Vendor must be 2 to 80 characters.')
    number = text('invoice_number')
    if not number:
        add('invoice_number', 'REQUIRED', 'Invoice number is required.')
    elif not re.fullmatch(r'[A-Z0-9-]{4,24}', number):
        add('invoice_number', 'INVALID_FORMAT', f'Use 4 to 24 characters: A-Z, 0-9, hyphen (received "{number}").')
    invoice_date = text('invoice_date')
    if not invoice_date:
        add('invoice_date', 'REQUIRED', 'Invoice date is required.')
    else:
        try:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', invoice_date):
                raise ValueError
            parsed = date.fromisoformat(invoice_date)
        except ValueError:
            add('invoice_date', 'INVALID_DATE_FORMAT', f'Use YYYY-MM-DD (received "{invoice_date}").')
        else:
            if parsed > (today or date.today()):
                add('invoice_date', 'DATE_IN_FUTURE', 'Invoice date cannot be after today.')
    amount = text('amount')
    if not amount:
        add('amount', 'REQUIRED', 'Amount is required.')
    elif not re.fullmatch(r'\d+(\.\d{1,2})?', amount) or Decimal(amount) <= 0:
        add('amount', 'INVALID_AMOUNT', f'Plain number above 0, max 2 decimals, no symbols or commas (received "{amount}").')
    currency = text('currency')
    if not currency:
        add('currency', 'REQUIRED', 'Currency is required.')
    elif currency not in CURRENCIES:
        add('currency', 'UNSUPPORTED_CURRENCY', f'Use one of {", ".join(CURRENCIES)} (received "{currency}").')
    category = text('category')
    if not category:
        add('category', 'REQUIRED', 'Category is required.')
    elif category not in CATEGORIES:
        add('category', 'INVALID_CATEGORY', f'"{category}" is not an allowed category.')
    cost_center = text('cost_center')
    if not cost_center:
        add('cost_center', 'REQUIRED', 'Cost center is required.')
    elif not re.fullmatch(r'CC-\d{3}', cost_center):
        add('cost_center', 'INVALID_FORMAT', f'Use CC-### (received "{cost_center}").')
    if len(text('description')) > 200:
        add('description', 'INVALID_FORMAT', 'Description is limited to 200 characters.')
    return errors


class PortalRejected(Exception):
    """The portal refused a submission; raised inside the traced step so PRISM records a failed call."""
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        """Keep the HTTP status and response body the portal returned."""
        codes = [e['code'] for e in body.get('errors', [])] or [body.get('error', 'REJECTED')]
        super().__init__(f'{status} ' + ', '.join(codes))
        self.status = status
        self.body = body
        self.codes = codes


class Portal:
    """Reports, expenses and the transfer-run log, stored locally."""
    def __init__(self, path: Path) -> None:
        """Create the portal tables if they do not exist yet."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS portal_reports (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS portal_expenses (
                    id TEXT PRIMARY KEY, report_id TEXT NOT NULL REFERENCES portal_reports(id),
                    claim TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflow_reads (
                    id TEXT PRIMARY KEY, version TEXT NOT NULL, name TEXT NOT NULL,
                    clean TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, report_id TEXT NOT NULL,
                    invoice TEXT NOT NULL, version TEXT NOT NULL, status TEXT NOT NULL,
                    codes TEXT NOT NULL, created_at TEXT NOT NULL
                );
            ''')

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Open a connection with named rows and foreign keys, committing on success."""
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys = ON')
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def new_report(self) -> str:
        """Open a fresh draft report and return its ID."""
        report_id = 'R-2026-' + uuid4().hex[:6].upper()
        with self.connect() as connection:
            connection.execute('INSERT INTO portal_reports VALUES (?, ?)', (report_id, now()))
        return report_id

    def has_report(self, report_id: str) -> bool:
        """Tell whether a report exists."""
        with self.connect() as connection:
            return connection.execute('SELECT 1 FROM portal_reports WHERE id = ?', (report_id,)).fetchone() is not None

    def submit(self, report_id: str, claim: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Validate and save one expense: 201 created, 422 field errors, 409 already on this report."""
        errors = validate(claim)
        if errors:
            return 422, {'error': 'VALIDATION_FAILED', 'errors': errors}
        record = {key: str(claim.get(key) or '').strip() for key in FIELDS}
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            for row in connection.execute('SELECT claim FROM portal_expenses WHERE report_id = ?', (report_id,)):
                existing = json.loads(row['claim'])
                if (existing['vendor'].casefold() == record['vendor'].casefold()
                        and existing['invoice_number'] == record['invoice_number']):
                    return 409, {'error': 'DUPLICATE_INVOICE',
                                 'errors': [{'field': 'invoice_number', 'code': 'DUPLICATE_INVOICE',
                                             'message': 'This vendor and invoice number are already on the report.'}]}
            count = connection.execute('SELECT COUNT(*) FROM portal_expenses').fetchone()[0]
            expense_id = f'EXP-2026-{count + 1:05d}'
            connection.execute('INSERT INTO portal_expenses VALUES (?, ?, ?, ?)',
                               (expense_id, report_id, json.dumps(record), now()))
        return 201, {'id': expense_id, 'status': 'pending_approval', 'claim': record}

    def report(self, report_id: str) -> dict[str, Any]:
        """Return a report with its expenses in submission order and totals per currency."""
        with self.connect() as connection:
            head = connection.execute('SELECT * FROM portal_reports WHERE id = ?', (report_id,)).fetchone()
            if head is None:
                raise ValueError('Unknown report.')
            rows = connection.execute('SELECT * FROM portal_expenses WHERE report_id = ? ORDER BY rowid',
                                      (report_id,)).fetchall()
        expenses = [{'id': r['id'], 'status': 'pending_approval', 'claim': json.loads(r['claim']),
                     'created_at': r['created_at']} for r in rows]
        totals: dict[str, Decimal] = {}
        for expense in expenses:
            claim = expense['claim']
            totals[claim['currency']] = totals.get(claim['currency'], Decimal('0')) + Decimal(claim['amount'])
        return {'id': report_id, 'created_at': head['created_at'], 'expenses': expenses,
                'totals': {currency: format(total, '.2f') for currency, total in totals.items()}}

    def remember_read(self, item_id: str, version: str, name: str, clean: dict[str, Any]) -> None:
        """Keep the normalized values of a read so a later transfer can be compared with them."""
        with self.connect() as connection:
            connection.execute('INSERT OR REPLACE INTO workflow_reads VALUES (?, ?, ?, ?, ?)',
                               (item_id, version, name, json.dumps(clean), now()))

    def read_clean(self, item_id: str) -> dict[str, Any] | None:
        """Return the normalized values remembered for a read, if any."""
        with self.connect() as connection:
            row = connection.execute('SELECT clean FROM workflow_reads WHERE id = ?', (item_id,)).fetchone()
        return json.loads(row['clean']) if row else None

    def record_run(self, session_id: str, report_id: str, invoice: str, version: str,
                   status: str, codes: list[str]) -> None:
        """Log one transfer run; the alert rule reads this log."""
        with self.connect() as connection:
            connection.execute('INSERT INTO workflow_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                               (str(uuid4()), session_id, report_id, invoice, version, status,
                                json.dumps(codes), now()))

    def alert(self) -> dict[str, Any]:
        """Evaluate the alert rule on the real log: failed runs above 20% of the last 10."""
        with self.connect() as connection:
            rows = connection.execute('SELECT status FROM workflow_runs ORDER BY rowid DESC LIMIT ?',
                                      (ALERT_WINDOW,)).fetchall()
        failed = sum(row['status'] == 'rejected' for row in rows)
        rate = failed / len(rows) if rows else 0.0
        return {'rule': f'Failed runs above {ALERT_THRESHOLD:.0%} of the last {ALERT_WINDOW}',
                'window': len(rows), 'failed': failed, 'rate': round(rate, 4), 'fired': rate > ALERT_THRESHOLD}


def now() -> str:
    """Current UTC time as ISO 8601."""
    return datetime.now(timezone.utc).isoformat()
