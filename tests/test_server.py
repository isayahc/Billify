"""HTTP upload and optional SDK contract checks without external services."""
import json
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from src.server import InvoiceHandler, ROOT
from src.invoices import process_csv


class ServerTests(unittest.TestCase):
    """Exercise the same upload endpoint used by the browser."""
    def test_upload_and_invalid_input(self) -> None:
        """Return useful downloads on success and a clear 400 for bad input."""
        temporary = tempfile.TemporaryDirectory()
        environment = patch.dict('os.environ', {'BILLIFY_DB_PATH': str(Path(temporary.name) / 'test.sqlite3')})
        environment.start()
        server = HTTPServer(('127.0.0.1', 0), InvoiceHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with urlopen(base + '/dashboard') as response:
                self.assertIn(b'Has errors', response.read())
            request = Request(base + '/api/import', json.dumps({'csv': (ROOT / 'data/invoices.csv').read_text()}).encode(), {'Content-Type': 'application/json'})
            with urlopen(request) as response:
                result = json.load(response)
                self.assertEqual(len(result['drafts']), 1)
                self.assertEqual(len(result['results']), 7)
                self.assertIn('PO_OVERBILLING', result['csv'])
            with urlopen(base + '/api/documents') as response:
                documents = json.load(response)['documents']
                self.assertEqual(len(documents), 7)
                self.assertTrue(all(d['filename'] == 'uploaded.csv' for d in documents))
            with urlopen(base + '/dashboard.js') as response:
                self.assertIn(b'/api/documents', response.read())
            bad = Request(base + '/api/import', b'{"csv":"bad"}', {'Content-Type': 'application/json'})
            with self.assertRaises(HTTPError) as error:
                urlopen(bad)
            self.assertEqual(error.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            environment.stop()
            temporary.cleanup()

    def test_prism_span_contract(self) -> None:
        """Verify real SDK payloads group seven tools under one invoice run."""
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

        # The callback methods do not require a model; permit dependency-light CI.
        with patch('prismtrace.langchain_handler._HAS_LANGCHAIN', True), patch.dict('os.environ', {}, clear=True):
            handler = PRISMtraceCallbackHandler(api_key='test-key', project_id='test-project')
        handler._client.close()
        handler._client = httpx.Client(transport=httpx.MockTransport(capture))
        try:
            results = process_csv((ROOT / 'data/invoices.csv').read_text(), json.loads((ROOT / 'data/reference.json').read_text()), handler)
        finally:
            handler.close()
        self.assertEqual(len(payloads), 7)
        self.assertEqual({p['session_id'] for p in payloads}, {r['session_id'] for r in results})
        for payload in payloads:
            tools = [s for s in payload['spans'] if s['span_type'] == 'tool']
            self.assertEqual(len(tools), 7)
            self.assertEqual(len({s['parent_span_id'] for s in tools}), 1)
            self.assertTrue(all(s['status'] == 'ok' for s in tools))


if __name__ == '__main__':
    unittest.main()
