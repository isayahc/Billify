"""The expense workflow: reading, cleanup, AP controls, the simulated portal and its endpoints, without Claude."""
import base64
import json
import tempfile
import threading
import unittest
from datetime import date
from http.server import HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from src.extract import ExtractionError
from src.portal import Portal, validate
from src.server import InvoiceHandler, ROOT
from src.workflow import canonical_invoice_number, clean_amount, extract_text, parse_date, read_items, transfer_items

TODAY = date(2026, 10, 17)
REFERENCE = json.loads((ROOT / 'data/workflow_reference.json').read_text(encoding='utf-8'))
SAMPLES = sorted((ROOT / 'data/sample_invoices').glob('*.txt'))
CLAIM = {'vendor': 'Acme Corporation', 'invoice_number': 'INV-0516', 'invoice_date': '2026-09-30', 'amount': '31.03',
         'currency': 'USD', 'category': 'Meals', 'cost_center': 'CC-210', 'description': 'Catered lunch, per head'}
# The planted traps and what catches them (README, "Sample invoices").
TRAPS = {'invoice_012.txt': {'UNKNOWN_VENDOR', 'MISSING_PO', 'BANK_DETAILS_CHANGED'},
         'invoice_036.txt': {'UNKNOWN_VENDOR', 'MISSING_PO', 'BANK_DETAILS_CHANGED'},
         'invoice_030.txt': {'UNKNOWN_VENDOR', 'UNKNOWN_PO'}, 'invoice_033.txt': {'UNKNOWN_VENDOR', 'UNKNOWN_PO'},
         'invoice_040.txt': {'MISSING_PO'}, 'invoice_041.txt': {'MISSING_PO'}, 'invoice_042.txt': {'MISSING_PO'},
         'invoice_045.txt': {'DATE_IN_FUTURE'}, 'invoice_013.txt': {'AMBIGUOUS_DATE'}, 'invoice_023.txt': {'AMBIGUOUS_DATE'},
         'invoice_016.txt': {'TOTAL_MISMATCH', 'PO_OVERBILLING'},
         **{f'invoice_{n}.txt': {'DUPLICATE_INVOICE'} for n in ('018', '021', '029', '034', '043', '050')}}


def text(name: str) -> str:
    """One sample invoice's text."""
    return (ROOT / 'data/sample_invoices' / name).read_text(encoding='utf-8')


def files(names: list[str] | None = None) -> list[dict[str, str]]:
    """Sample invoices as the page sends them."""
    return [{'name': p.name, 'source': 'Folder', 'text': p.read_text(encoding='utf-8')}
            for p in SAMPLES if names is None or p.name in names]


def claims(items: list[dict]) -> list[dict]:
    """What the page transfers: every invoice that is not held, with the fields shown for review."""
    return [{'id': i['id'], 'name': i['name'], 'fields': i['fields']} for i in items if not i['hold']]


class ReaderTests(unittest.TestCase):
    """Read sample invoices as written, then normalize without guessing."""
    def test_formats(self) -> None:
        """Dotted dates, decimal commas, currency symbols and variant invoice numbers."""
        kessler = extract_text(text('invoice_006.txt'))
        self.assertEqual((kessler['invoice_date'], kessler['amount'], kessler['currency']), ('10.09.2026', '8.315,00', 'EUR'))
        self.assertEqual(parse_date(kessler['invoice_date'], kessler['region']), ('2026-09-10', 'day.month.year'))
        self.assertEqual(clean_amount(kessler['amount']), '8315.00')
        summit = extract_text(text('invoice_011.txt'))
        self.assertEqual((summit['amount'], summit['currency']), ('3,066.00', 'CAD'))
        resent = extract_text(text('invoice_018.txt'))
        self.assertEqual(resent['invoice_number'], 'INV 563')
        self.assertEqual(parse_date(resent['invoice_date'], resent['region']), ('2026-10-08', 'US vendor, read month first'))
        for written, canonical in [('INV 563', 'INV-0563'), ('inv-902', 'INV-0902'), ('No. 0564', 'INV-0564'),
                                   ('Invoice #881', 'INV-0881'), ('INV0870', 'INV-0870'), ('#0866', 'INV-0866'),
                                   ('BL-7221', 'BL-7221')]:
            self.assertEqual(canonical_invoice_number(written), canonical)
        for written, amount in [('1.250,00', '1250.00'), ('$1,250.00', '1250.00'), ('1.250', '1250.00'), ('12', '12.00'),
                                ('12.5', '12.50'), ('abc', '')]:
            self.assertEqual(clean_amount(written), amount)

    def test_never_guesses_a_date(self) -> None:
        """A slash date that reads both ways is left empty unless the vendor is in the US."""
        for name, written in [('invoice_013.txt', '09/10/2026'), ('invoice_023.txt', '04/10/2026')]:
            raw = extract_text(text(name))
            self.assertEqual((raw['invoice_date'], raw['region']), (written, 'non-us'))
            iso, why = parse_date(raw['invoice_date'], raw['region'])
            self.assertEqual(iso, '')
            self.assertIn('not guessed', why)
        self.assertEqual(parse_date('09/10/2026', 'us')[0], '2026-09-10')
        self.assertEqual(parse_date('09/10/2026', '')[0], '')
        self.assertEqual(parse_date('13/10/2026', '')[0], '2026-10-13')
        self.assertEqual(parse_date('02/30/2026', 'us')[0], '')
        self.assertEqual(parse_date('14 October 2026', 'non-us')[0], '2026-10-14')


class PortalTests(unittest.TestCase):
    """The simulated Acme Expense portal enforces its own rules."""
    def test_rules_and_duplicates(self) -> None:
        """201 for a clean claim, 422 with every field error, 409 for the same invoice on one report."""
        with tempfile.TemporaryDirectory() as tmp:
            portal = Portal(Path(tmp) / 'portal.sqlite3')
            report = portal.new_report()
            status, body = portal.submit(report, CLAIM)
            self.assertEqual((status, body['id'], body['status']), (201, 'EXP-2026-00001', 'pending_approval'))
            status, body = portal.submit(report, {**CLAIM, 'vendor': 'ACME CORPORATION'})
            self.assertEqual((status, body['errors'][0]['code']), (409, 'DUPLICATE_INVOICE'))
            self.assertEqual(portal.submit(portal.new_report(), CLAIM)[0], 201)
            status, body = portal.submit(report, {**CLAIM, 'invoice_date': '09/30/2026', 'amount': '1,031.03', 'currency': '$',
                                                  'category': 'Catering', 'cost_center': ''})
            self.assertEqual(status, 422)
            self.assertEqual({(e['field'], e['code']) for e in body['errors']},
                             {('invoice_date', 'INVALID_DATE_FORMAT'), ('amount', 'INVALID_AMOUNT'),
                              ('currency', 'UNSUPPORTED_CURRENCY'), ('category', 'INVALID_CATEGORY'), ('cost_center', 'REQUIRED')})
            saved = portal.report(report)
            self.assertEqual((len(saved['expenses']), saved['totals']), (1, {'USD': '31.03'}))
            with self.assertRaisesRegex(ValueError, 'Unknown report'):
                portal.report('R-2026-NOPE')
        self.assertEqual([e['code'] for e in validate({**CLAIM, 'invoice_date': '2026-10-18'}, TODAY)], ['DATE_IN_FUTURE'])
        self.assertEqual(validate(CLAIM, TODAY), [])

    def test_alert_rule(self) -> None:
        """The alert fires when more than 20% of the last 10 transfer runs failed."""
        with tempfile.TemporaryDirectory() as tmp:
            portal = Portal(Path(tmp) / 'portal.sqlite3')
            self.assertFalse(portal.alert()['fired'])
            for status in ['created'] * 8 + ['rejected'] * 2:
                portal.record_run('s', 'R', 'invoice.txt', 'v1', status, [])
            self.assertEqual((portal.alert()['failed'], portal.alert()['fired']), (2, False))
            portal.record_run('s', 'R', 'invoice.txt', 'v1', 'rejected', ['INVALID_DATE_FORMAT'])
            alert = portal.alert()
            self.assertEqual((alert['window'], alert['failed'], alert['rate'], alert['fired']), (10, 3, 0.3, True))


class WorkflowTests(unittest.TestCase):
    """Read, check and transfer the 50 sample invoices with both agent versions."""
    def test_v1_against_v2(self) -> None:
        """Same reads and holds; v1's as-written values are rejected, v2's normalized values are created."""
        with tempfile.TemporaryDirectory() as tmp:
            portal = Portal(Path(tmp) / 'portal.sqlite3')
            runs = {}
            for version in ('v1', 'v2'):
                read = read_items({'version': version, 'default_cost_center': 'CC-210', 'files': files()},
                                  REFERENCE, portal, today=TODAY)
                runs[version] = (read['items'], transfer_items({'version': version, 'items': claims(read['items'])}, portal))
        held = {version: {i['name'] for i in items if i['hold']} for version, (items, _) in runs.items()}
        self.assertEqual(held['v1'], set(TRAPS))
        self.assertEqual(held['v2'], set(TRAPS))
        v1, v2 = runs['v1'][1], runs['v2'][1]
        self.assertEqual(v1['summary'], {'sent': 33, 'created': 0, 'rejected': 33})
        self.assertEqual(v2['summary'], {'sent': 33, 'created': 33, 'rejected': 0})
        self.assertEqual({c['code']: c['runs'] for c in v1['causes']},
                         {'INVALID_DATE_FORMAT': 33, 'INVALID_CATEGORY': 33, 'REQUIRED': 33, 'INVALID_AMOUNT': 24})
        self.assertEqual((v1['root_cause']['failed'], v1['root_cause']['would_pass']), (33, 33))
        self.assertIsNone(v2['root_cause'])
        self.assertTrue(v1['alert']['fired'])
        self.assertFalse(v2['alert']['fired'])
        self.assertEqual(len(v2['report']['expenses']), 33)
        self.assertEqual({(r['steps'][-1]['name'], r['steps'][-1]['status'], r['http']) for r in v1['results']},
                         {('submit_expense', 'error', 422)})

    def test_traps_are_caught(self) -> None:
        """Each planted trap is held by the finding meant to catch it; the first copy of a re-sent invoice passes."""
        with tempfile.TemporaryDirectory() as tmp:
            items = read_items({'files': files(), 'default_cost_center': 'CC-210'}, REFERENCE,
                               Portal(Path(tmp) / 'portal.sqlite3'), today=TODAY)['items']
        found = {i['name']: {c['code'] for c in i['controls'] + i['checks']} for i in items}
        for name, codes in TRAPS.items():
            self.assertLessEqual(codes, found[name], name)
        for name in ('invoice_008.txt', 'invoice_007.txt', 'invoice_019.txt', 'invoice_027.txt', 'invoice_020.txt',
                     'invoice_049.txt', 'invoice_001.txt', 'invoice_017.txt', 'invoice_011.txt'):
            self.assertEqual(found[name], set(), name)
        steps = next(i for i in items if i['name'] == 'invoice_018.txt')['steps']
        self.assertEqual([s['name'] for s in steps if s['run'] == 'validate_invoice'],
                         ['load_invoice', 'normalize_fields', 'lookup_vendor', 'lookup_purchase_order', 'search_existing_bills',
                          'validate_amounts', 'save_decision'])
        self.assertIn('DUPLICATE_INVOICE', next(s for s in steps if s['name'] == 'search_existing_bills')['output'])

    def test_pdf_without_text(self) -> None:
        """A PDF without a text layer goes to the PDF reader; without Claude it asks for pasted text."""
        pdf = base64.b64encode(b'%PDF-1.4\n% scanned page\n').decode()
        fields = {'is_invoice': True, 'vendor_name': 'Acme Corporation', 'invoice_number': 'INV-0601',
                  'invoice_date': '2026-10-01', 'due_date': '', 'po_number': 'PO-4534', 'subtotal': '20.00', 'tax': '0.00',
                  'total': '20.00', 'currency': 'USD', 'ambiguities': [], 'model': 'fake',
                  'as_written': {'invoice_date': 'October 1, 2026', 'due_date': '', 'subtotal': '$20.00', 'tax': '',
                                 'total': '$20.00', 'currency': '$'}}

        def unavailable(data: bytes, name: str) -> dict:
            """Behave like extract.read_pdf with no credentials."""
            raise ExtractionError('Claude credentials are missing: set ANTHROPIC_API_KEY on the server.', 503)

        with tempfile.TemporaryDirectory() as tmp:
            portal = Portal(Path(tmp) / 'portal.sqlite3')

            def read(reader) -> dict:
                """Read the one scanned file with the given PDF reader."""
                return read_items({'files': [{'name': 'scan.pdf', 'pdf': pdf}], 'default_cost_center': 'CC-210'},
                                  REFERENCE, portal, read_pdf_fn=reader, today=TODAY)['items'][0]

            item = read(lambda data, name: fields)
            self.assertEqual(item['status'], 'read')
            self.assertEqual({k: item['clean'][k] for k in ('invoice_date', 'amount', 'currency')},
                             {'invoice_date': '2026-10-01', 'amount': '20.00', 'currency': 'USD'})
            self.assertEqual(item['written']['amount'], '$20.00')
            self.assertEqual(item['steps'][0]['output'], 'scan.pdf · read by fake')
            missing = read(unavailable)
            self.assertEqual(missing['status'], 'needs_text')
            self.assertEqual(missing['steps'][0]['status'], 'error')
            self.assertEqual(read(lambda data, name: {'is_invoice': False})['status'], 'not_invoice')
            empty = read_items({'files': [{'name': 'photo.png'}]}, REFERENCE, portal, today=TODAY)['items'][0]
            self.assertEqual(empty['status'], 'needs_text')

    def test_bad_requests(self) -> None:
        """Malformed read and transfer requests fail with a message instead of partial work."""
        with tempfile.TemporaryDirectory() as tmp:
            portal = Portal(Path(tmp) / 'portal.sqlite3')
            for payload, message in [({'files': []}, '1 to 60'), ({'version': 'v3', 'files': files(['invoice_001.txt'])}, 'v1 or v2'),
                                     ({'default_cost_center': '210', 'files': files(['invoice_001.txt'])}, 'CC-210'),
                                     ({'files': [{'name': 'a.pdf', 'pdf': base64.b64encode(b'hello').decode()}]}, 'not a PDF'),
                                     ({'files': [{'name': 'a.pdf', 'pdf': '%%%'}]}, 'not valid base64'), ([], 'JSON object')]:
                with self.assertRaisesRegex(ValueError, message):
                    read_items(payload, REFERENCE, portal)
            for payload, message in [({'items': []}, '1 to 60'), ({'items': [{'id': 'x'}]}, 'id and fields'),
                                     ({'report_id': 'R-2026-NOPE', 'items': [{'id': 'x', 'fields': CLAIM}]}, 'Unknown report')]:
                with self.assertRaisesRegex(ValueError, message):
                    transfer_items(payload, portal)

    def test_prism_runs(self) -> None:
        """Each invoice's read, checks and transfer share one PRISM session; a rejected submit is a failed tool span."""
        try:
            import httpx
            from prismtrace import PRISMtraceCallbackHandler
        except ImportError:
            self.skipTest('Install requirements.txt to test the PRISM SDK contract.')
        payloads = []

        def capture(request: 'httpx.Request') -> 'httpx.Response':
            """Capture the SDK HTTP payload without accessing a live account."""
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json={'ok': True})

        with patch('prismtrace.langchain_handler._HAS_LANGCHAIN', True), patch.dict('os.environ', {}, clear=True):
            handler = PRISMtraceCallbackHandler(api_key='test-key', project_id='test-project')
        handler._client.close()
        handler._client = httpx.Client(transport=httpx.MockTransport(capture))
        try:
            with tempfile.TemporaryDirectory() as tmp:
                portal = Portal(Path(tmp) / 'portal.sqlite3')
                item = read_items({'version': 'v1', 'files': files(['invoice_001.txt'])}, REFERENCE, portal, handler,
                                  today=TODAY)['items'][0]
                transfer_items({'version': 'v1', 'items': claims([item])}, portal, handler)
        finally:
            handler.close()
        self.assertEqual({p['session_id'] for p in payloads}, {item['id']})
        runs = {}
        for payload in payloads:
            root = next(s for s in payload['spans'] if s['span_type'] == 'chain' and not s['parent_span_id'])
            tools = [s for s in payload['spans'] if s['span_type'] == 'tool']
            self.assertEqual({s['parent_span_id'] for s in tools}, {root['span_id']})
            runs[root['name']] = (root['status'], {s['name']: s['status'] for s in tools})
        self.assertEqual(set(runs), {'read_invoice', 'validate_invoice', 'transfer_invoice'})
        self.assertEqual(len(runs['read_invoice'][1]), 8)
        self.assertEqual(len(runs['validate_invoice'][1]), 7)
        self.assertEqual(runs['transfer_invoice'], ('error', {'fill_portal_form': 'ok', 'submit_expense': 'error'}))


class EndpointTests(unittest.TestCase):
    """The page's endpoints, served the way the browser calls them."""
    def setUp(self) -> None:
        """Serve on a free port against a temporary database."""
        self.temporary = tempfile.TemporaryDirectory()
        self.environment = patch.dict('os.environ', {'BILLIFY_DB_PATH': str(Path(self.temporary.name) / 'test.sqlite3')})
        self.environment.start()
        self.server = HTTPServer(('127.0.0.1', 0), InvoiceHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self) -> None:
        """Stop the server and remove the database."""
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.environment.stop()
        self.temporary.cleanup()

    def post(self, path: str, body: object, headers: dict | None = None) -> dict:
        """POST JSON and decode the reply."""
        request = Request(self.base + path, json.dumps(body).encode(), {'Content-Type': 'application/json', **(headers or {})})
        with urlopen(request) as response:
            return json.load(response)

    def status(self, path: str, body: object = None, headers: dict | None = None) -> int:
        """The HTTP status of a request that is expected to fail."""
        data = None if body is None else json.dumps(body).encode()
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(self.base + path, data, {'Content-Type': 'application/json', **(headers or {})}))
        return error.exception.code

    def test_flow(self) -> None:
        """Samples, read, pre-check, transfer and report, then the errors a page can run into."""
        with urlopen(self.base + '/') as response:
            self.assertIn(b'Review what Billify read', response.read())
        with urlopen(self.base + '/workflow.js') as response:
            self.assertIn(b'/api/transfer', response.read())
        with urlopen(self.base + '/api/samples') as response:
            samples = json.load(response)
        self.assertEqual((len(samples['folder']), len(samples['inbox'])), (51, 7))
        self.assertIn('Facilities', samples['categories'])
        read = self.post('/api/read', {'version': 'v2', 'default_cost_center': 'CC-210',
                                       'files': [f for f in samples['folder'] if f['name'] in ('invoice_001.txt', 'invoice_008.txt')]})
        self.assertEqual([(i['status'], i['hold'], i['precheck']) for i in read['items']], [('read', False, [])] * 2)
        self.assertFalse(read['tracing_enabled'])
        issues = self.post('/api/precheck', {'fields': {**read['items'][0]['fields'], 'invoice_date': '09/30/2026'}})['issues']
        self.assertEqual([e['code'] for e in issues], ['INVALID_DATE_FORMAT'])
        moved = self.post('/api/transfer', {'version': 'v2', 'items': claims(read['items'])})
        self.assertEqual(moved['summary'], {'sent': 2, 'created': 2, 'rejected': 0})
        again = self.post('/api/transfer', {'version': 'v2', 'report_id': moved['report']['id'], 'items': claims(read['items'][:1])})
        self.assertEqual((again['results'][0]['http'], again['results'][0]['errors'][0]['code']), (409, 'DUPLICATE_INVOICE'))
        with urlopen(self.base + '/api/portal/report?id=' + moved['report']['id']) as response:
            report = json.load(response)
        self.assertEqual((len(report['expenses']), report['totals']), (2, {'USD': '10371.03'}))
        self.assertEqual(self.status('/api/read', {'files': []}), 400)
        self.assertEqual(self.status('/api/precheck', {'claim': {}}), 400)
        self.assertEqual(self.status('/api/transfer', {'report_id': 'R-2026-NOPE', 'items': claims(read['items'])}), 400)
        self.assertEqual(self.status('/api/read', {'files': []}, {'Origin': 'http://example.com'}), 403)
        self.assertEqual(self.status('/api/read', {'files': []}, {'Content-Type': 'text/plain'}), 400)
        self.assertEqual(self.status('/api/precheck', {'fields': {'description': 'x' * 70_000}}), 400)
        self.assertEqual(self.status('/api/portal/report?id=R-2026-NOPE'), 404)
        self.assertEqual(self.status('/api/portal/report'), 400)


if __name__ == '__main__':
    unittest.main()
