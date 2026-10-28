"""Hidden password input and optional OS credential stores; no plaintext fallback."""
import ctypes
import getpass
import hashlib
import sys


class CredentialError(RuntimeError):
    """A credential is missing or the system store is unavailable."""


def _service(profile):
    # Separate installations and accounts never share a password lookup accidentally.
    identity = hashlib.sha256(profile["portal"].rstrip("/").encode()).hexdigest()[:20]
    return "org.westlake-aistation." + identity


def _security():
    library = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    library.SecKeychainAddGenericPassword.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
        ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]
    library.SecKeychainAddGenericPassword.restype = ctypes.c_int32
    library.SecKeychainFindGenericPassword.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
        ctypes.c_uint32, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p)]
    library.SecKeychainFindGenericPassword.restype = ctypes.c_int32
    library.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    library.SecKeychainItemFreeContent.restype = ctypes.c_int32
    library.SecKeychainItemModifyAttributesAndData.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
    library.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
    return library


def _mac_read(profile):
    lib = _security()
    service, account = _service(profile).encode(), profile["account"].encode()
    length, pointer = ctypes.c_uint32(), ctypes.c_void_p()
    status = lib.SecKeychainFindGenericPassword(None, len(service), service, len(account), account,
                                               ctypes.byref(length), ctypes.byref(pointer), None)
    if status == -25300:  # errSecItemNotFound
        return None
    if status:
        raise CredentialError(f"System Keychain access failed (OSStatus {status}); check OS/sandbox access")
    try:
        return ctypes.string_at(pointer, length.value).decode()
    finally:
        lib.SecKeychainItemFreeContent(None, pointer)


def _mac_store(profile, password):
    lib = _security()
    service, account, value = _service(profile).encode(), profile["account"].encode(), password.encode()
    status = lib.SecKeychainAddGenericPassword(None, len(service), service, len(account), account,
                                              len(value), value, None)
    if status == -25299:  # Existing item: update inside the same secure store.
        item = ctypes.c_void_p()
        status = lib.SecKeychainFindGenericPassword(None, len(service), service, len(account), account,
                                                   None, None, ctypes.byref(item))
        if not status:
            try:
                status = lib.SecKeychainItemModifyAttributesAndData(item, None, len(value), value)
            finally:
                core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
                core.CFRelease.argtypes = [ctypes.c_void_p]
                core.CFRelease(item)
    if status:
        raise CredentialError(f"System Keychain storage failed (OSStatus {status})")


def _system_keyring():
    try:
        import keyring
        backend = keyring.get_keyring()
    except Exception:
        return None
    module = type(backend).__module__
    approved = ("keyring.backends.SecretService", "keyring.backends.kwallet", "keyring.backends.Windows", "keyring.backends.macOS")
    # In particular reject keyrings.alt plaintext files and uninspected chained backends.
    if not any(module == name or module.startswith(name + ".") for name in approved):
        return None
    return backend


def _read_stored(profile):
    if sys.platform == "darwin":
        return _mac_read(profile)
    backend = _system_keyring()
    if backend is None:
        return None
    try:
        return backend.get_password(_service(profile), profile["account"])
    except Exception:
        raise CredentialError("System credential-store access failed") from None


def get_password(profile, interactive=True):
    password = _read_stored(profile)
    if password:
        return password
    if not interactive:
        raise CredentialError("No saved system-store credential; use login --remember before background submission")
    if not sys.stdin.isatty():
        raise CredentialError("Hidden password input requires an interactive terminal; use login --remember")
    try:
        password = getpass.getpass("AIStation password (hidden): ")
    except (EOFError, KeyboardInterrupt):
        raise CredentialError("Password input cancelled") from None
    if not password:
        raise CredentialError("Password must not be empty")
    return password


def store_password(profile, password):
    if not isinstance(password, str) or not password:
        raise CredentialError("Password must not be empty")
    if sys.platform == "darwin":
        return _mac_store(profile, password)
    backend = _system_keyring()
    if backend is None:
        raise CredentialError("No supported system credential store; install keyring and enable Secret Service/KWallet, or use foreground hidden input")
    try:
        backend.set_password(_service(profile), profile["account"], password)
    except Exception:
        raise CredentialError("System credential-store write failed") from None


def password_available(profile):
    return bool(_read_stored(profile))
