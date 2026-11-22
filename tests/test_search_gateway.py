"""Unit tests for ``custom_tools.search_gateway`` against a stub gateway.

Run: ``python -m unittest discover -s tests`` (needs ``httpx`` only).

Tree
----
Stub(BaseHTTPRequestHandler)            records requests, replies REPLIES
SearchGatewayTest(unittest.TestCase)
    setUpClass() / tearDownClass()
    run_async(coro) -> str
    test_search_sends_snippet_query_and_formats()
    test_search_empty_names_down_engines()
    test_fetch_window_and_paging()
    test_fetch_excerpts()
    test_errors_are_error_text()
"""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from custom_tools import search_gateway as sg  # noqa: E402

SEEN, REPLIES = [], {}


class Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        SEEN.append((self.path, body, self.headers.get('Authorization')))
        status, reply = REPLIES[self.path]
        data = json.dumps(reply).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        pass


class SearchGatewayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Stub)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.cfg = {'base_url': f'http://127.0.0.1:{cls.server.server_port}/',
                   'api_key': 'k', 'max_results': 3, 'max_chars': 500}

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def run_async(self, coro):
        SEEN.clear()
        return asyncio.run(coro)

    def test_search_sends_snippet_query_and_formats(self):
        REPLIES['/v2/search'] = (200, {'sources': [
            {'title': 'T', 'url': 'https://a.example/', 'snippet': 's',
             'score': 1, 'document': {'status': 'skipped'}}]})
        out = self.run_async(sg.search('q', 'week', self.cfg))
        self.assertEqual(json.loads(out), [
            {'title': 'T', 'url': 'https://a.example/', 'snippet': 's'}])
        path, body, auth = SEEN[0]
        self.assertEqual((path, auth), ('/v2/search', 'Bearer k'))
        self.assertEqual(body, {'query': 'q', 'fetch_top': 0,
                                'max_results': 3, 'time_range': 'week'})

    def test_search_empty_names_down_engines(self):
        REPLIES['/v2/search'] = (200, {'sources': [], 'unresponsive_engines':
                                       [['brave', 'timeout']]})
        out = json.loads(self.run_async(sg.search('q', None, self.cfg)))
        self.assertEqual(out['results'], [])
        self.assertIn('brave', out['note'])
        self.assertNotIn('time_range', SEEN[0][1])

    def test_fetch_window_and_paging(self):
        REPLIES['/v2/fetch'] = (200, {
            'status': 'success', 'url': 'https://a.example/x',
            'final_url': 'https://a.example/y', 'title': 'Doc',
            'warnings': [], 'content': 'abc', 'offset': 10,
            'next_offset': 13, 'total_chars': 40})
        out = self.run_async(sg.fetch('https://a.example/x', None, 10,
                                      self.cfg))
        self.assertEqual(out, 'URL: https://a.example/y\nTitle: Doc\n'
                         'Characters 10-13 of 40.\n\nabc\n\n[more: call '
                         'web_fetch again with offset=13]')
        self.assertEqual(SEEN[0][1], {'url': 'https://a.example/x',
                                      'offset': 10, 'max_chars': 500})

    def test_fetch_excerpts(self):
        REPLIES['/v2/fetch'] = (200, {
            'status': 'success', 'url': 'u', 'final_url': 'u', 'title': '',
            'warnings': [], 'content': 'x', 'offset': None,
            'next_offset': None, 'total_chars': 99, 'excerpts': [
                {'offset': 5, 'text': 'one'}, {'offset': 50, 'text': 'two'}]})
        out = self.run_async(sg.fetch('https://a.example/', 'q', 0,
                                      self.cfg))
        self.assertIn('2 excerpts matching the query from 99', out)
        self.assertIn('[offset 5]\none', out)
        self.assertIn('[offset 50]\ntwo', out)
        self.assertEqual(SEEN[0][1]['query'], 'q')

    def test_errors_are_error_text(self):
        REPLIES['/v2/fetch'] = (200, {
            'status': 'failed', 'url': 'https://a.example/',
            'error': {'code': 'http_404', 'detail': 'page not found'}})
        out = self.run_async(sg.fetch('https://a.example/', None, 0,
                                      self.cfg))
        self.assertEqual(out, 'Error: could not read https://a.example/: '
                         'http_404: page not found')
        REPLIES['/v2/fetch'] = (400, {'detail': {'code': 'unsafe_url'}})
        out = self.run_async(sg.fetch('http://10.0.0.1/', None, 0,
                                      self.cfg))
        self.assertTrue(out.startswith('Error: search gateway HTTP 400'))
        self.assertIn('unsafe_url', out)
        down = {**self.cfg, 'base_url': 'http://127.0.0.1:1'}
        out = self.run_async(sg.search('q', None, down))
        self.assertTrue(out.startswith('Error: search gateway unreachable'))


if __name__ == '__main__':
    unittest.main()
