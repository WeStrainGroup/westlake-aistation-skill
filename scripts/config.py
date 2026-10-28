"""User-owned, nonsecret profile configuration for the portable AIStation CLI."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import ssl
import tempfile
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    """The local profile is incomplete or unsafe to use."""


_CONNECTION_REQUIRED = {"portal", "account", "ca_file", "certificate_sha256"}
_EXECUTION_REQUIRED = {"ssh_host_fingerprint", "development_group_id", "image"}
_REQUIRED = _CONNECTION_REQUIRED | _EXECUTION_REQUIRED | {"persistent_root"}
_OPTIONAL = {"framework", "card_type", "ssh_host", "ssh_user", "remote_python",
             "max_environment_seconds", "training_group_id", "storage", "storage_mount"}
_SECRET_FIELDS = {"password", "passwd", "token", "access_token", "authorization", "secret"}


def default_profile_path():
    explicit = os.environ.get("WLAI_PROFILE")
    if explicit:
        return Path(explicit).expanduser().resolve()
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "westlake-aistation" / "profile.json"


def _text(value, field):
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise ConfigurationError(f"{field} must be a nonempty string without control characters")
    return value.strip()


def validate_profile(source, base_dir=None, require_execution=True):
    if not isinstance(source, dict):
        raise ConfigurationError("Profile must be a JSON object")
    if any(str(k).lower() in _SECRET_FIELDS for k in source):
        raise ConfigurationError("Do not put credentials in the profile; use hidden input or the system credential store")
    unknown = set(source) - _REQUIRED - _OPTIONAL
    if unknown:
        raise ConfigurationError("Profile contains unsupported fields")
    missing = _CONNECTION_REQUIRED - set(source)
    if missing:
        raise ConfigurationError("Missing profile fields: " + ", ".join(sorted(missing)))
    profile = {key: _text(source[key], key) for key in _CONNECTION_REQUIRED}
    if profile['account'] in ('.', '..') or any(char in profile['account'] for char in ('/', '\\')):
        raise ConfigurationError('account cannot contain path separators or parent components')
    for key in _EXECUTION_REQUIRED:
        if source.get(key):
            profile[key] = _text(source[key], key)
    try:
        url = urlsplit(profile["portal"])
        port = url.port
    except ValueError:
        raise ConfigurationError("portal must be an HTTPS origin") from None
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.path not in ("", "/") or url.query or url.fragment:
        raise ConfigurationError("portal must be an HTTPS origin without credentials, path, query or fragment")
    if port is not None and not 1 <= port <= 65535:
        raise ConfigurationError("portal port is out of range")
    profile["portal"] = profile["portal"].rstrip("/")
    fingerprint = profile["certificate_sha256"].replace(":", "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ConfigurationError("certificate_sha256 must be the verified certificate's 64 hexadecimal SHA-256 digits")
    profile["certificate_sha256"] = fingerprint
    if 'ssh_host_fingerprint' in profile:
        if not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}=?", profile["ssh_host_fingerprint"]):
            raise ConfigurationError("ssh_host_fingerprint must be an OpenSSH SHA256 fingerprint")
        profile["ssh_host_fingerprint"] = profile["ssh_host_fingerprint"].rstrip("=")
    ca_file = Path(profile["ca_file"]).expanduser()
    if not ca_file.is_absolute():
        ca_file = Path(base_dir or Path.cwd()) / ca_file
    ca_file = ca_file.resolve()
    if not ca_file.is_file():
        raise ConfigurationError("ca_file must name an existing, independently verified CA/certificate file")
    profile["ca_file"] = str(ca_file)
    account = profile['account']
    master, ssdwork = '/' + account, '/ssdwork/' + account
    old_root = source.get('persistent_root')
    if old_root is not None:
        old_root = _text(old_root, 'persistent_root')
    def inside(path, parent):
        return path == parent or path.startswith(parent + '/')
    inferred = 'ssdwork' if old_root and inside(old_root, ssdwork) else 'master'
    if old_root and not inside(old_root, master) and not inside(old_root, ssdwork):
        inferred = 'custom'
    storage = source.get('storage', inferred)
    if storage not in ('master', 'ssdwork', 'custom'):
        raise ConfigurationError('storage must be master, ssdwork or custom')
    expected_mount = master if storage == 'master' else ssdwork if storage == 'ssdwork' else None
    mount = source.get('storage_mount', expected_mount)
    if mount is None:
        raise ConfigurationError('Custom storage requires an explicit storage_mount verified inside the environment')
    mount = _text(mount, 'storage_mount')
    root_text = old_root or mount + '/wlai_runs'
    for key, value in (('storage_mount', mount), ('persistent_root', root_text)):
        path = PurePosixPath(value)
        if not path.is_absolute() or '..' in path.parts or str(path) == '/':
            raise ConfigurationError(key + " must be an absolute personal POSIX directory without '..'")
        if path.parts[1] in ('tmp', 'var', 'root', 'run', 'dev', 'proc', 'sys'):
            raise ConfigurationError(key + ' is an ephemeral or system path, not a verified persistent mount')
    mount, root_text = str(PurePosixPath(mount)), str(PurePosixPath(root_text))
    if expected_mount and mount != expected_mount:
        raise ConfigurationError('storage_mount does not match the chosen personal storage')
    if not inside(root_text, mount) or root_text == mount:
        raise ConfigurationError('persistent_root must be a child directory inside storage_mount')
    profile.update(storage=storage, storage_mount=mount, persistent_root=root_text)
    defaults = {"framework": "pytorch", "card_type": "", "ssh_host": url.hostname,
                "ssh_user": "root", "remote_python": "/opt/conda/bin/python"}
    for key, default in defaults.items():
        value = source.get(key, default)
        profile[key] = "" if key == "card_type" and value == "" else _text(value, key)
    if not PurePosixPath(profile["remote_python"]).is_absolute() or ".." in PurePosixPath(profile["remote_python"]).parts:
        raise ConfigurationError("remote_python must be an absolute POSIX executable path without '..'")
    if any(c in profile["ssh_host"] for c in ("/", "@", " ", "\\")):
        raise ConfigurationError("ssh_host must be a hostname or IP address")
    limit = source.get("max_environment_seconds", 14400)
    if type(limit) is not int or not 61 <= limit <= 14400:
        raise ConfigurationError("max_environment_seconds must be an integer from 61 through 14400")
    profile["max_environment_seconds"] = limit
    if "training_group_id" in source:
        profile["training_group_id"] = _text(source["training_group_id"], "training_group_id")
    if require_execution:
        missing = execution_requirements(profile)
        if missing:
            raise ConfigurationError('Execution configuration incomplete: ' + ', '.join(missing) + '; use configure, or resources --group all for discovery')
    return profile


def load_profile(path=None, require_execution=True):
    target = Path(path).expanduser().resolve() if path else default_profile_path()
    try:
        content = target.read_text(encoding="utf-8")
        source = json.loads(content)
    except FileNotFoundError:
        raise ConfigurationError("Profile not found; run init or pass --profile with your own configuration") from None
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ConfigurationError("Could not read profile as a UTF-8 JSON object") from None
    profile = validate_profile(source, target.parent, require_execution=require_execution)
    profile["_path"] = str(target.resolve())
    return profile


def execution_requirements(profile):
    return sorted(key for key in _EXECUTION_REQUIRED if not profile.get(key))


def certificate_pin(path):
    """Derive a leaf pin from one independently trusted PEM, never from the network."""
    target = Path(path).expanduser().resolve()
    try:
        pem = target.read_text(encoding='ascii')
        if pem.count('-----BEGIN CERTIFICATE-----') != 1:
            raise ValueError('Expected one server certificate')
        return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
    except (OSError, UnicodeError, ValueError):
        raise ConfigurationError('Provide one independently verified server certificate PEM, or set the leaf pin explicitly') from None


def write_profile(path, profile, replace=False):
    requested = Path(path).expanduser()
    target = requested.parent.resolve() / requested.name
    if not replace and (target.exists() or target.is_symlink()):
        raise FileExistsError('Profile already exists; use configure to update it')
    values = {key: value for key, value in profile.items() if not key.startswith('_')}
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', dir=target.parent, delete=False) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            json.dump(values, handle, indent=2)
            handle.write('\n'); handle.flush(); os.fsync(handle.fileno())
        if replace:
            os.replace(temporary, target)
        else:
            os.link(temporary, target)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return target


def profile_identity(profile):
    return hashlib.sha256((profile["portal"].rstrip("/") + "\n" + profile["account"]).encode()).hexdigest()[:20]


def state_directory(profile):
    root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))).expanduser()
    return root / "westlake-aistation" / profile_identity(profile)
