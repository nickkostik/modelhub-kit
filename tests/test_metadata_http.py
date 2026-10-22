import datetime
import email.message
import email.utils
import http.client
import io
import json
import pathlib
import tempfile
import unittest
import urllib.error
from unittest.mock import call, patch

from modelhub_kit import catalog, core


URL = 'https://catalog.example.invalid/models'


class Response(io.BytesIO):
    url = URL

    def __init__(self, body=b'{"models":[{"id":"exact-model"}]}'):
        super().__init__(body)
        self.headers = email.message.Message()
        self.headers['Content-Type'] = 'application/json'


def rejection(status, code=None, retry_after=None, body=None, kind=None):
    headers = email.message.Message()
    headers['Content-Type'] = 'application/json'
    if retry_after is not None:
        headers['Retry-After'] = retry_after
    if body is None:
        body = json.dumps({'error': {'code': code, 'type': kind,
                                    'message': 'do-not-emit-provider-detail'}}).encode()
    return urllib.error.HTTPError(URL, status, 'do-not-emit-provider-detail', headers, io.BytesIO(body))


class MetadataHTTPTests(unittest.TestCase):
    def test_temporary_rejection_reuses_exact_get_headers_and_closes_error_before_wait(self):
        error = rejection(429, retry_after='2')
        response = Response()
        with patch.object(core.urllib.request, 'build_opener') as opener, \
                patch.object(core.time, 'sleep', side_effect=lambda delay: self.assertTrue(error.fp.closed)) as sleep:
            opener.return_value.open.side_effect = [error, response]
            value, _ = core.http_json(URL, {'Authorization': 'Bearer fixture', 'If-None-Match': 'fixture-revision'})
        self.assertEqual(value['models'][0]['id'], 'exact-model')
        sleep.assert_called_once_with(2)
        calls = opener.return_value.open.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertIs(calls[0].args[0], calls[1].args[0])
        request = calls[0].args[0]
        self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(request.full_url, URL)
        self.assertEqual(request.get_header('Authorization'), 'Bearer fixture')
        self.assertEqual(request.get_header('If-none-match'), 'fixture-revision')
        self.assertIsNone(request.data)

    def test_two_retries_bound_temporary_unavailability(self):
        errors = [rejection(503) for _ in range(3)]
        with patch.object(core.urllib.request, 'build_opener') as opener, patch.object(core.time, 'sleep') as sleep:
            opener.return_value.open.side_effect = errors
            with self.assertRaises(core.MetadataHTTPError) as raised:
                core.http_json(URL)
        self.assertEqual(opener.return_value.open.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(0.25), call(0.5)])
        self.assertEqual(raised.exception.diagnostic(), {'httpStatus': 503, 'category': 'temporarily-unavailable', 'attempts': 3})
        self.assertTrue(all(error.fp.closed for error in errors))

    def test_explicit_temporary_code_can_explain_400_without_retrying_generic_400(self):
        with patch.object(core.urllib.request, 'build_opener') as opener, patch.object(core.time, 'sleep'):
            opener.return_value.open.side_effect = [rejection(400, 'temporarily_unavailable', kind='invalid_request_error'), Response()]
            core.http_json(URL)
        self.assertEqual(opener.return_value.open.call_count, 2)
        for status, code, category in ((400, None, 'invalid-request'),
                                        (400, 'unsupported_parameter', 'unsupported-parameter'),
                                        (400, 'invalid_request_error', 'invalid-request'),
                                        (400, 'invalid_api_key', 'credential-or-permission'),
                                        (401, 'temporarily_unavailable', 'credential-or-permission'),
                                        (403, None, 'credential-or-permission'),
                                        (429, 'insufficient_quota', 'credential-or-permission'),
                                        (503, 'authentication_error', 'credential-or-permission'),
                                        (503, 'unrecognized-credential-error', 'request-rejected'),
                                        (500, None, 'request-rejected'),
                                        (502, None, 'request-rejected'),
                                        (504, None, 'request-rejected')):
            with self.subTest(status=status, code=code), patch.object(core.urllib.request, 'build_opener') as opener, \
                    patch.object(core.time, 'sleep') as sleep:
                opener.return_value.open.side_effect = rejection(status, code, retry_after='1')
                with self.assertRaises(core.MetadataHTTPError) as raised:
                    core.http_json(URL)
            self.assertEqual(raised.exception.category, category)
            self.assertEqual(opener.return_value.open.call_count, 1)
            sleep.assert_not_called()

    def test_retry_after_date_is_respected_and_long_or_malformed_waits_are_deferred(self):
        epoch = 1800000000
        future = email.utils.format_datetime(datetime.datetime.fromtimestamp(epoch + 3, datetime.timezone.utc), usegmt=True)
        with patch.object(core.urllib.request, 'build_opener') as opener, \
                patch.object(core.time, 'time', return_value=epoch), patch.object(core.time, 'sleep') as sleep:
            opener.return_value.open.side_effect = [rejection(503, retry_after=future), Response()]
            core.http_json(URL)
        sleep.assert_called_once_with(3)
        for retry_after in ('120', 'invalid-date', '-1', '1.5', '9' * 130):
            with self.subTest(retry_after=retry_after), patch.object(core.urllib.request, 'build_opener') as opener, \
                    patch.object(core.time, 'sleep') as sleep:
                opener.return_value.open.side_effect = rejection(429, retry_after=retry_after)
                with self.assertRaises(core.MetadataHTTPError) as raised:
                    core.http_json(URL)
            self.assertEqual(opener.return_value.open.call_count, 1)
            sleep.assert_not_called()
            if retry_after == '120':
                self.assertEqual(raised.exception.diagnostic()['retryAfterSeconds'], 120)

    def test_network_errors_and_started_or_incomplete_bodies_are_never_replayed(self):
        for error in (urllib.error.URLError('fixture network failure'), TimeoutError('fixture timeout')):
            with self.subTest(error=error), patch.object(core.urllib.request, 'build_opener') as opener, \
                    patch.object(core.time, 'sleep') as sleep:
                opener.return_value.open.side_effect = error
                with self.assertRaises(type(error)):
                    core.http_json(URL)
            self.assertEqual(opener.return_value.open.call_count, 1)
            sleep.assert_not_called()
        for response in (Response(b'{"models":'), Response()):
            with self.subTest(response=response), patch.object(core.urllib.request, 'build_opener') as opener, \
                    patch.object(core.time, 'sleep') as sleep:
                opener.return_value.open.return_value = response
                response.headers['Content-Length'] = str(len(response.getvalue()) + 1)
                with self.assertRaises(core.GuardError):
                    core.http_json(URL)
            self.assertEqual(opener.return_value.open.call_count, 1)
            sleep.assert_not_called()
        error = rejection(503)
        with patch.object(error, 'read', side_effect=http.client.IncompleteRead(b'partial')), \
                patch.object(core.urllib.request, 'build_opener') as opener, patch.object(core.time, 'sleep') as sleep:
            opener.return_value.open.side_effect = error
            with self.assertRaises(core.MetadataHTTPError):
                core.http_json(URL)
        self.assertEqual(opener.return_value.open.call_count, 1)
        sleep.assert_not_called()

    def test_not_modified_closes_response_without_retry_or_invalidating_cache(self):
        error = rejection(304)
        with patch.object(core.urllib.request, 'build_opener') as opener, patch.object(core.time, 'sleep') as sleep:
            opener.return_value.open.side_effect = error
            self.assertEqual(core.http_json(URL, {'If-None-Match': 'fixture-revision'}), (None, {}))
        self.assertTrue(error.fp.closed)
        self.assertEqual(opener.return_value.open.call_count, 1)
        sleep.assert_not_called()

    def test_catalog_reports_sanitized_400_and_preserves_last_good_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp)
            settings = {'catalog_sources': [{'id': 'example', 'url': URL}]}
            catalog.sync(settings, state, lambda *args: ({'models': [{'id': 'exact-model', 'reasoning_efforts': ['max', 'future-choice']}]}, {}))
            file = state / 'catalogs/example.json'
            before = file.read_bytes()
            with patch.object(core.urllib.request, 'build_opener') as opener, patch.object(core.time, 'sleep') as sleep:
                opener.return_value.open.side_effect = rejection(400, 'unknown_parameter')
                result = catalog.sync(settings, state, core.http_json)
            self.assertEqual(result[0]['status'], 'refresh-failed-last-good-retained')
            self.assertEqual(result[0]['diagnostic'], {'httpStatus': 400, 'category': 'unsupported-parameter', 'attempts': 1})
            self.assertNotIn('do-not-emit', json.dumps(result))
            self.assertEqual(file.read_bytes(), before)
            sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
