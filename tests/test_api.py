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
