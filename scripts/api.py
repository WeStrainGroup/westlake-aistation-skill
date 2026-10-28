"""Pinned HTTPS AIStation client with connection reuse and bounded read retries.

Observed AIStation endpoints, not an official vendor SDK. Credentials, cookies and
session tokens remain in process memory. Mutations are never automatically retried.
"""
import hashlib
import errno
import http.client
from http.cookies import SimpleCookie
import json
import random
import re
import ssl
import threading
import time
from urllib.parse import urlencode, urlsplit


class ApiError(RuntimeError):
    def __init__(self, message, *, status=None, code=None, server_message=""):
        super().__init__(message)
        self.status = status
        self.code = code
        # Deliberately do not retain vendor error bodies, which can echo secrets.
        self.server_message = ""


class IdentityError(ssl.SSLError):
    """The verified server identity has changed; never retry or bypass it."""


class AIStation:
    def __init__(self, profile, timeout=20, password_provider=None, *, read_attempts=3, retry_budget=30):
        if timeout <= 0 or read_attempts < 1 or retry_budget <= 0:
            raise ValueError("HTTP timeout, attempt count and retry budget must be positive")
        self.profile = profile
        self.timeout = timeout
        self.read_attempts = min(int(read_attempts), 5)
        self.retry_budget = float(retry_budget)
        self.password_provider = password_provider
        self.token = ""
        self.cookies = {}
        self.timings = []
        self.extra_headers = {}
        self.connection = None
        self._lock = threading.RLock()
        self._logging_in = False
        url = urlsplit(profile["portal"])
        self.host, self.port = url.hostname, url.port or 443
        self.context = ssl.create_default_context(cafile=profile["ca_file"])
        # Some deployments use vendor certificates for a different hostname. Full
        # chain validation AND the explicitly configured leaf pin are required.
        self.context.check_hostname = False
        if not hasattr(ssl, "VERIFY_X509_PARTIAL_CHAIN"):
            raise ApiError("This Python/OpenSSL runtime lacks partial-chain verification; upgrade the runtime before using this client")
        self.context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2

    def close(self):
        with self._lock:
            if self.connection is not None:
                self.connection.close()
                self.connection = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _connect(self, timeout):
        if self.connection is None:
            self.connection = http.client.HTTPSConnection(self.host, self.port, context=self.context, timeout=timeout)
        connection = self.connection
        connection.timeout = timeout
        if connection.sock is None:
            connection.connect()
        connection.sock.settimeout(timeout)
        observed = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
        if observed != self.profile["certificate_sha256"]:
            self.close()
            raise IdentityError("Server certificate differs from the configured pin; independently verify it before changing trust")
        return connection

    @staticmethod
    def _transient(error):
        if isinstance(error, ssl.SSLError):
            return False
        return isinstance(error, (TimeoutError, ConnectionError, http.client.RemoteDisconnected,
                                  http.client.IncompleteRead, http.client.CannotSendRequest,
                                  http.client.ResponseNotReady)) or getattr(error, "errno", None) in (
                                      errno.ECONNRESET, errno.ETIMEDOUT, errno.EHOSTUNREACH,
                                      errno.ENETDOWN, errno.ENETUNREACH, errno.ECONNREFUSED, errno.EPIPE)

    def _pause(self, attempt, deadline):
        delay = min(4.0, 0.4 * 2 ** attempt) + random.uniform(0, 0.2)
        remaining = deadline - time.monotonic()
        if remaining <= delay:
            return False
        time.sleep(delay)
        return True

    def _read_body(self, response, sock, deadline):
        chunks = []
        size = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("HTTP response deadline exhausted")
            sock.settimeout(min(self.timeout, remaining))
            block = response.read1(65536)
            if not block:
                if getattr(response, "length", None) not in (None, 0):
                    raise http.client.IncompleteRead(b"")
                return b"".join(chunks)
            size += len(block)
            if size > 32 * 1024 * 1024:
                self.close()
                raise ApiError("HTTP JSON response exceeds the 32 MiB safety limit; use file transfer for large outputs")
            chunks.append(block)

    def call(self, method, path, *, params=None, body=None):
        method = method.upper()
        if method not in ("GET", "POST", "PUT", "DELETE", "PATCH"):
            raise ValueError("Unsupported HTTP method")
        if not isinstance(path, str) or not path.startswith("/api/") or any(c in path for c in ("?", "#", "\r", "\n")):
            raise ValueError("Expected a platform /api/ path without query or fragment")
        target = path + ("?" + urlencode(params) if params else "")
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode() if body is not None else None
        started = time.monotonic()
        deadline = started + self.retry_budget
        attempts = self.read_attempts if method == "GET" else 1
        refreshed = False
        attempt = 0
        # Single reusable connection; serialize callers to preserve HTTP framing.
        with self._lock:
            try:
                while True:
                    attempt += 1
                    remaining = deadline - time.monotonic()
                    if method == "GET" and remaining <= 0:
                        raise ApiError("Read retry deadline exhausted; no additional request sent")
                    timeout = min(self.timeout, remaining) if method == "GET" else self.timeout
                    try:
                        request_deadline = deadline if method == "GET" else time.monotonic() + self.timeout
                        connection = self._connect(timeout)
                        headers = {"Accept": "application/json", "X-Accept-Language": "zh_CN"}
                        if self.token:
                            headers["X-Auth-Token"] = self.token
                        if self.cookies:
                            headers["Cookie"] = "; ".join(k + "=" + v for k, v in self.cookies.items())
                        if payload is not None:
                            headers["Content-Type"] = "application/json;charset=UTF-8"
                        headers.update(self.extra_headers)
                        connection.request(method, target, body=payload, headers=headers)
                        sock = connection.sock
                        response = connection.getresponse()
                        raw = self._read_body(response, sock, request_deadline)
                        status = response.status
                        for key, value in response.getheaders():
                            if key.lower() == "set-cookie":
                                cookie = SimpleCookie()
                                try:
                                    cookie.load(value)
                                except Exception:
                                    raise ApiError("Malformed session cookie") from None
                                self.cookies.update({k: v.value for k, v in cookie.items()})
                        if status == 401 and method == "GET" and not refreshed and not self._logging_in and self.password_provider:
                            refreshed = True
                            self.token = ""; self.cookies.clear(); self.close()
                            password = self.password_provider()
                            try:
                                self.login(self.profile["account"], password)
                            finally:
                                del password
                            # One auth refresh is independent of transient retry count.
                            continue
                        if status in (502, 503, 504) and method == "GET" and attempt < attempts:
                            self.close()
                            if self._pause(attempt - 1, deadline):
                                continue
                        if status != 200:
                            raise ApiError(f"HTTP {status} at {path}; mutation requests are never replayed", status=status)
                        try:
                            data = json.loads(raw)
                        except (json.JSONDecodeError, UnicodeError):
                            raise ApiError("Platform returned invalid JSON; body suppressed") from None
                        if not isinstance(data, dict) or not data.get("flag"):
                            raise ApiError("Platform rejected request; response body suppressed", code="PLATFORM_REJECTED")
                        return data.get("resData")
                    except (OSError, http.client.HTTPException) as error:
                        self.close()
                        if method == "GET" and attempt < attempts and self._transient(error) and self._pause(attempt - 1, deadline):
                            continue
                        if isinstance(error, ssl.SSLError):
                            raise IdentityError("TLS identity validation failed; trust was not bypassed") from None
                        raise ApiError("HTTP transport failed; request result may be unknown; mutations are never replayed") from None
            finally:
                self.timings.append({"method": method, "path": path, "attempts": attempt,
                                     "seconds": round(time.monotonic() - started, 3)})
                # Bounded diagnostics never include queries, bodies, headers or tokens.
                del self.timings[:-100]

    def login(self, account, password):
        from gmssl.sm2 import CryptSM2
        if not isinstance(account, str) or not account or not isinstance(password, str) or not password:
            raise ApiError("Account and password must not be empty")
        with self._lock:
            previous = self._logging_in
            self._logging_in = True
            try:
                public = self.call("GET", "/api/ibase/v1/system/secret")
                if not isinstance(public, str):
                    raise ApiError("Unexpected public key response")
                public = public[2:] if len(public) == 130 and public.startswith("04") else public
                if not re.fullmatch(r"[0-9a-fA-F]{128}", public):
                    raise ApiError("Unexpected SM2 public key")
                cipher = CryptSM2(public_key=public, private_key="", mode=0)
                encrypted = "04" + cipher.encrypt(password.encode()).hex()
                user = self.call("POST", "/api/ibase/v1/login", body={"account": account, "password": encrypted})
                if not isinstance(user, dict) or not isinstance(user.get("token"), str) or not user["token"]:
                    raise ApiError("Login did not return a session token")
                self.token = user["token"]
                self.cookies["Access_Token"] = self.token
                return {key: user.get(key) for key in ("account", "userId", "groupId")}
            finally:
                self._logging_in = previous

    def environments(self):
        return self.call("GET", "/api/iresource/v1/work-platform/history", params={"page": 1, "pageSize": 20})

    def task(self, task_id, status_flag=3):
        return self.call("GET", "/api/iresource/v1/train", params={"id": task_id, "statusFlag": status_flag, "page": 1, "pageSize": 1})

    def task_log(self, task_id):
        return self.call("GET", f"/api/iresource/v1/train/{task_id}/read-log")


def rows(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("data", "items", "list", "records"):
            if isinstance(value.get(key), list):
                return value[key]
    return []
