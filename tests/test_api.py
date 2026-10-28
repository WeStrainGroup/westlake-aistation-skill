"""Offline HTTP behavior tests; fake endpoints and certificates only."""
import hashlib
import http.client
import json
from pathlib import Path
import ssl
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from api import AIStation, ApiError, IdentityError


CERT = b"synthetic-test-certificate"
PROFILE = {"portal": "https://cluster.example.org", "account": "example-user",
           "ca_file": "unused-test-file", "certificate_sha256": hashlib.sha256(CERT).hexdigest()}


class Socket:
    def settimeout(self, _):
        pass
    def getpeercert(self, binary_form=False):
        return CERT


class Response:
    def __init__(self, data=None, status=200, raw=None):
        self.status = status
        self.raw = json.dumps({"flag": True, "resData": data}).encode() if raw is None else raw
    def getheaders(self):
        return []
    def read1(self, _):
        data, self.raw = self.raw, b""
        return data


class Connection:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.sock = None
        self.requests = []
        self.connects = 0
    def connect(self):
        self.connects += 1
        self.sock = Socket()
    def request(self, method, target, body=None, headers=None):
        self.requests.append((method, target, body, headers))
    def getresponse(self):
        result = self.outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result
    def close(self):
        self.sock = None


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.context = Mock()
        self.context.verify_flags = 0
        self.patch_ssl = patch("api.ssl.create_default_context", return_value=self.context)
        self.patch_ssl.start()
        self.patch_sleep = patch("api.time.sleep")
        self.patch_sleep.start()
    def tearDown(self):
        self.patch_sleep.stop()
        self.patch_ssl.stop()
    def make(self, outcomes, **kwargs):
        connection = Connection(outcomes)
        factory = patch("api.http.client.HTTPSConnection", return_value=connection)
        self.addCleanup(factory.stop)
        mock = factory.start()
        return AIStation(PROFILE, **kwargs), connection, mock
    def test_reuses_one_verified_tls_connection(self):
        api, connection, factory = self.make([Response(1), Response(2)])
        self.assertEqual(api.call("GET", "/api/first"), 1)
        self.assertEqual(api.call("GET", "/api/second"), 2)
        self.assertEqual(connection.connects, 1)
        self.assertEqual(factory.call_count, 1)
        api.close()
        self.assertIsNone(api.connection)
    def test_connect_and_request_do_not_restart_outer_deadline_for_headers(self):
        now = [0.0]
        api, connection, _ = self.make([Response('ok')], deadline_provider=lambda: 6.0)
        connect = connection.connect
        request = connection.request
        observed_timeouts = []
        def slow_connect():
            connect(); now[0] += 3
            connection.sock.settimeout = observed_timeouts.append
        def slow_request(*args, **kwargs):
            request(*args, **kwargs); now[0] += 2
        connection.connect = slow_connect
        connection.request = slow_request
        with patch('api.time.monotonic', side_effect=lambda: now[0]):
            self.assertEqual(api.call('POST', '/api/test'), 'ok')
        self.assertEqual(len(connection.requests), 1)
        self.assertEqual(observed_timeouts[0], 6.0)
        self.assertEqual(observed_timeouts[1], 3.0)
        self.assertTrue(all(value <= 1 for value in observed_timeouts[2:]))
    def test_exhausted_outer_deadline_after_tls_sends_no_mutation(self):
        now = [0.0]
        api, connection, _ = self.make([Response('unreachable')], deadline_provider=lambda: 5.0)
        connect = connection.connect
        def slow_connect():
            connect(); now[0] = 6.0
        connection.connect = slow_connect
        with patch('api.time.monotonic', side_effect=lambda: now[0]):
            with self.assertRaises(ApiError):
                api.call('POST', '/api/test')
        self.assertEqual(connection.requests, [])
        self.assertEqual(connection.connects, 1)
    def test_read_retries_transient_failure_and_gateway(self):
        api, connection, _ = self.make([http.client.RemoteDisconnected("synthetic"), Response(status=503), Response("ok")])
        self.assertEqual(api.call("GET", "/api/test"), "ok")
        self.assertEqual(len(connection.requests), 3)
        self.assertEqual(api.timings[-1]["attempts"], 3)
    def test_read_retry_count_is_bounded(self):
        api, connection, _ = self.make([Response(status=503)] * 3)
        with self.assertRaises(ApiError):
            api.call("GET", "/api/test")
        self.assertEqual(len(connection.requests), 3)
    def test_mutations_never_replay_even_after_ambiguous_disconnect(self):
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            with self.subTest(method=method):
                api, connection, _ = self.make([http.client.RemoteDisconnected("sensitive-response"), Response("unexpected")])
                with self.assertRaises(ApiError) as failure:
                    api.call(method, "/api/test", body={"opaque": "hidden-value"})
                self.assertEqual(len(connection.requests), 1)
                self.assertNotIn("sensitive-response", str(failure.exception))
                self.assertNotIn("hidden-value", str(failure.exception))
    def test_mutation_401_never_refreshes_or_replays(self):
        provider = Mock(return_value="synthetic-secret")
        api, connection, _ = self.make([Response(status=401)], password_provider=provider)
        with self.assertRaises(ApiError):
            api.call("POST", "/api/test")
        provider.assert_not_called()
        self.assertEqual(len(connection.requests), 1)
    def test_get_auth_refreshes_only_once(self):
        provider = Mock(return_value="synthetic-secret")
        api, connection, _ = self.make([Response(status=401), Response(status=401)], password_provider=provider)
        api.login = Mock(return_value={"account": PROFILE["account"]})
        with self.assertRaises(ApiError):
            api.call("GET", "/api/test")
        provider.assert_called_once_with()
        api.login.assert_called_once_with(PROFILE["account"], "synthetic-secret")
        self.assertEqual(len(connection.requests), 2)
    def test_certificate_mismatch_blocks_http_data_and_never_retries(self):
        api, connection, _ = self.make([Response("unreachable")])
        api.profile = dict(PROFILE, certificate_sha256="0" * 64)
        with self.assertRaises(IdentityError):
            api.call("GET", "/api/test")
        self.assertEqual(connection.requests, [])
        self.assertEqual(connection.connects, 1)
    def test_vendor_error_body_and_error_code_are_not_echoed(self):
        raw = json.dumps({"flag": False, "errCode": "SYNTHETIC_SECRET", "errMessage": "synthetic-secret", "resData": {"password": "synthetic-secret"}}).encode()
        api, _, _ = self.make([Response(raw=raw)])
        with self.assertRaises(ApiError) as failure:
            api.call("GET", "/api/test", params={"k": "synthetic-query-secret"})
        message = repr(failure.exception) + repr(api.timings) + failure.exception.server_message
        for secret in ("SYNTHETIC_SECRET", "synthetic-secret", "synthetic-query-secret"):
            self.assertNotIn(secret, message)
        self.assertEqual(failure.exception.code, 'PLATFORM_REJECTED')
        self.assertEqual(failure.exception.operation, '/api/test')
    def test_known_vendor_codes_are_retained_with_only_local_safe_messages(self):
        for code in ('IRESOURCE_MAX_WOKR_PLATFORM_COUNT_EXCEED',
                     'IRESOURCE_INPUT_GPU_MORE_THAN_WORK_PLATFORM_CONFIG_LIMIT',
                     'IRESOURCE_CREATE_WORK_PLATFORM_RES_GROUP_NOT_MATCH_LABEL',
                     'IRESOURCE_COMMON_RES_GROUP_CAN_NOT_BE_USED',
                     'COMMON_PARAMETER_NOT_MATCH'):
            raw = json.dumps({'flag': False, 'errCode': code, 'errMessage': 'synthetic-response-secret',
                              'resData': {'password': 'synthetic-response-secret'}}).encode()
            api, connection, _ = self.make([Response(raw=raw)])
            with self.subTest(code=code), self.assertRaises(ApiError) as failure:
                api.call('POST', '/api/iresource/v1/work-platform/',
                         params={'k': 'synthetic-query-secret'}, body={'password': 'synthetic-request-secret'})
            error = failure.exception
            self.assertEqual(error.code, code)
            self.assertEqual(error.operation, '/api/iresource/v1/work-platform/')
            self.assertEqual(error.method, 'POST')
            self.assertIn(code, str(error))
            self.assertTrue(error.safe_server_message)
            diagnostics = str(error) + repr(error.__dict__) + repr(api.timings)
            for secret in ('synthetic-response-secret', 'synthetic-query-secret', 'synthetic-request-secret'):
                self.assertNotIn(secret, diagnostics)
            self.assertEqual(len(connection.requests), 1)
    def test_vendor_code_validation_is_bounded_and_has_no_raw_message_fallback(self):
        prefix = 'ISTORAGE_'
        accepted = prefix + 'X' * (128 - len(prefix))
        for raw_code, expected in [(accepted, accepted), (accepted + 'X', 'PLATFORM_REJECTED'),
                                   ('IBASE_REQUEST_REJECTED_1', 'IBASE_REQUEST_REJECTED_1'),
                                   ('IIMAGE_REQUEST_REJECTED_1', 'IIMAGE_REQUEST_REJECTED_1'),
                                   ('IRESOURCE_lowercase_secret', 'PLATFORM_REJECTED'),
                                   ('COMMON_BAD\nsynthetic-secret', 'PLATFORM_REJECTED'),
                                   ('UNRELATED_SECRET', 'PLATFORM_REJECTED'),
                                   (None, 'PLATFORM_REJECTED')]:
            raw = json.dumps({'flag': False, 'errCode': raw_code, 'errMessage': 'synthetic-secret'}).encode()
            api, _, _ = self.make([Response(raw=raw)])
            with self.subTest(raw_code=raw_code), self.assertRaises(ApiError) as failure:
                api.call('GET', '/api/test')
            self.assertEqual(failure.exception.code, expected)
            self.assertEqual(failure.exception.safe_server_message, '')
            self.assertNotIn('synthetic-secret', str(failure.exception) + repr(failure.exception.__dict__))
    def test_401_and_transport_timeout_preserve_original_operation_without_query(self):
        for outcome in (Response(status=401), TimeoutError('synthetic-transport-secret')):
            api, connection, _ = self.make([outcome])
            with self.subTest(outcome=type(outcome).__name__), self.assertRaises(ApiError) as failure:
                api.call('POST', '/api/iresource/v1/train', params={'k': 'synthetic-query-secret'})
            self.assertEqual(failure.exception.operation, '/api/iresource/v1/train')
            self.assertEqual(failure.exception.method, 'POST')
            if isinstance(outcome, Response):
                self.assertEqual(failure.exception.status, 401)
            self.assertEqual(len(connection.requests), 1)
            diagnostics = str(failure.exception) + repr(failure.exception.__dict__) + repr(api.timings)
            self.assertNotIn('synthetic-query-secret', diagnostics)
            self.assertNotIn('synthetic-transport-secret', diagnostics)
    def test_raw_server_message_argument_cannot_populate_diagnostics(self):
        error = ApiError('Rejected', code='IRESOURCE_MAX_WOKR_PLATFORM_COUNT_EXCEED',
                         server_message='synthetic-secret', operation='/api/test?k=synthetic-query-secret', method='POST')
        self.assertEqual(error.operation, '/api/test')
        self.assertTrue(error.safe_server_message)
        self.assertEqual(error.server_message, error.safe_server_message)
        self.assertNotIn('synthetic-secret', repr(error.__dict__))
        self.assertNotIn('synthetic-query-secret', repr(error.__dict__))
    def test_invalid_json_body_suppressed(self):
        api, _, _ = self.make([Response(raw=b"synthetic-sensitive-non-json")])
        with self.assertRaises(ApiError) as failure:
            api.call("GET", "/api/test")
        self.assertNotIn("synthetic-sensitive-non-json", str(failure.exception))
    def test_read_deadline_prevents_extra_attempt(self):
        api, connection, _ = self.make([Response("unreachable")], retry_budget=0.01)
        with patch("api.time.monotonic", side_effect=[0.0, 0.02, 0.02]):
            with self.assertRaises(ApiError):
                api.call("GET", "/api/test")
        self.assertEqual(connection.requests, [])
    def test_read_permission_error_is_not_retried(self):
        api, connection, _ = self.make([PermissionError("synthetic-sensitive-error")])
        with self.assertRaises(ApiError) as failure:
            api.call("GET", "/api/test")
        self.assertEqual(len(connection.requests), 1)
        self.assertNotIn("synthetic-sensitive-error", str(failure.exception))
    def test_tls_failures_do_not_retry(self):
        api, connection, _ = self.make([ssl.SSLCertVerificationError("synthetic")])
        with self.assertRaises(IdentityError):
            api.call("GET", "/api/test")
        self.assertEqual(len(connection.requests), 1)


if __name__ == "__main__":
    unittest.main()
