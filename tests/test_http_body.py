import email.message
import gzip
import http.client
import io
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from modelhub_kit import catalog, core


class Response(io.BytesIO):
    url = 'https://catalog.example.invalid/models'

    def __init__(self, body, encoding=None, length=None):
        super().__init__(body)
        self.headers = email.message.Message()
        self.headers['Content-Type'] = 'application/json'
        self.headers['ETag'] = 'fixture-revision'
        if encoding:
            self.headers['Content-Encoding'] = encoding
        if length is not None:
            self.headers['Content-Length'] = str(length)


class MetadataBodyTests(unittest.TestCase):
    def test_truncated_compressed_and_declared_bodies_retain_last_good_catalog(self):
        body = b'{"models":[{"id":"exact-model","reasoning_efforts":["max"]}]}'
        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp)
            settings = {'catalog_sources': [{'id': 'example', 'url': Response.url}]}
            catalog.sync(settings, state, lambda *args: ({'models': [{'id': 'prior-model'}]}, {}))
            before = (state / 'catalogs/example.json').read_bytes()
            for response in (Response(gzip.compress(body)[:-5], 'gzip'), Response(body, length=len(body) + 1)):
                with self.subTest(response=response), patch.object(core.urllib.request, 'build_opener') as opener:
                    opener.return_value.open.return_value = response
                    result = catalog.sync(settings, state, core.http_json)
                self.assertEqual(result[0]['status'], 'refresh-failed-last-good-retained')
                self.assertEqual((state / 'catalogs/example.json').read_bytes(), before)
            response = Response(body)
            with patch.object(response, 'read', side_effect=http.client.IncompleteRead(b'partial')), \
                    patch.object(core.urllib.request, 'build_opener') as opener:
                opener.return_value.open.return_value = response
                with self.assertRaises(core.GuardError):
                    core.http_json(Response.url)


if __name__ == '__main__':
    unittest.main()
