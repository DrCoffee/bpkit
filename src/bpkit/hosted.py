"""Client for the hosted image-builder service on console.redhat.com.

The hosted API takes JSON in its own shape (``ssh_key`` rather than ``key``,
``min_size`` in bytes, ``fips.enabled``, …). :func:`to_compose_request`
converts a blueprint to it, and refuses settings the service can't express
instead of silently dropping them.

Authentication uses Red Hat SSO, with one of:

* an **offline token** (https://access.redhat.com/management/api), in
  ``RH_OFFLINE_TOKEN`` or ``offline_token=``, or
* a **service account** (console.redhat.com → Service Accounts), in
  ``RH_CLIENT_ID`` / ``RH_CLIENT_SECRET``.

::

    from bpkit.hosted import HostedClient

    client = HostedClient.from_env()
    compose_id = client.compose(WebServer(), "guest-image")
    status = client.wait(compose_id)
    client.download(compose_id, "out/")       # aws.s3 uploads give a URL

Only the standard library is used.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .models import parse_size

if TYPE_CHECKING:
    from .blueprint import Blueprint

__all__ = [
    "DEFAULT_API_URL",
    "DEFAULT_SSO_URL",
    "HostedAuth",
    "HostedClient",
    "HostedError",
    "HostedUnsupportedError",
    "to_compose_request",
    "to_hosted_customizations",
]

DEFAULT_API_URL = "https://console.redhat.com/api/image-builder/v1"
DEFAULT_SSO_URL = "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token"


class HostedError(RuntimeError):
    """The hosted service (or SSO) returned an error."""

    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class HostedUnsupportedError(ValueError):
    """The blueprint uses settings the hosted service can't express."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__(
            "the hosted image-builder service can't express:\n  - "
            + "\n  - ".join(problems)
            + "\n(pass lossy=True / --lossy to drop them)"
        )
        self.problems = problems


# --------------------------------------------------------------------------
# Blueprint → hosted JSON
# --------------------------------------------------------------------------

# Top-level customizations the hosted schema accepts as-is (from extra_customizations).
_PASSTHROUGH = {
    "openscap",
    "fdo",
    "ignition",
    "disk",
    "cacerts",
    "subscription",
    "aap_registration",
    "installation_device",
    "partitioning_mode",
    "hostname",
}

_REPO_KEYS = {
    "id": "id",
    "name": "name",
    "filename": "filename",
    "baseurls": "baseurl",
    "gpgkeys": "gpgkey",
    "metalink": "metalink",
    "mirrorlist": "mirrorlist",
    "gpgcheck": "check_gpg",
    "repo_gpgcheck": "check_repo_gpg",
    "enabled": "enabled",
    "priority": "priority",
    "sslverify": "ssl_verify",
    "module_hotfixes": "module_hotfixes",
}


def _pick(d: Mapping[str, Any], keys: Mapping[str, str], what: str, problems: list[str]) -> dict:
    out = {}
    for k, v in d.items():
        if k in keys:
            out[keys[k]] = v
        else:
            problems.append(f"{what}: {k!r}")
    return out


def to_hosted_customizations(bp: Mapping[str, Any], *, lossy: bool = False) -> dict[str, Any]:
    """Convert a blueprint dict (``Blueprint.to_dict()``) to hosted ``customizations``."""
    problems: list[str] = []
    out: dict[str, Any] = {}
    c = dict(bp.get("customizations", {}))

    packages: list[str] = []
    for p in [*bp.get("packages", []), *bp.get("modules", [])]:
        version = p.get("version", "*")
        packages.append(p["name"] if version in ("*", "", None) else f"{p['name']}-{version}")
    packages += [f"@{g['name']}" for g in bp.get("groups", [])]
    if packages:
        out["packages"] = packages
    if mods := bp.get("enabled_modules"):
        for m in mods:
            if not m.get("stream"):
                problems.append(f"enabled_modules: {m['name']!r} needs a stream")
        out["enabled_modules"] = [m for m in mods if m.get("stream")]
    if containers := bp.get("containers"):
        out["containers"] = [
            _pick(
                x,
                {"source": "source", "name": "name", "tls-verify": "tls_verify"},
                f"container {x['source']}",
                problems,
            )
            for x in containers
        ]

    users: dict[str, dict[str, Any]] = {}
    for u in c.pop("user", []):
        users[u["name"]] = _pick(
            u,
            {"name": "name", "groups": "groups", "key": "ssh_key", "password": "password"},
            f"user {u['name']}",
            problems,
        )
    for k in c.pop("sshkey", []):
        user = users.setdefault(k["user"], {"name": k["user"]})
        if "ssh_key" in user and user["ssh_key"] != k["key"]:
            problems.append(f"sshkey for {k['user']!r}: user already has a different key")
        else:
            user["ssh_key"] = k["key"]
    if users:
        out["users"] = list(users.values())
    if groups := c.pop("group", None):
        out["groups"] = groups

    if kernel := c.pop("kernel", None):
        out["kernel"] = _pick(kernel, {"name": "name", "append": "append"}, "kernel", problems)
    for key in ("timezone", "locale", "services"):
        if key in c:
            value = c.pop(key)
            if key == "services" and "default_target" in value:
                problems.append("services: 'default_target'")
                value = {k: v for k, v in value.items() if k != "default_target"}
            out[key] = value
    if fw := c.pop("firewall", None):
        if "zones" in fw:
            problems.append("firewall: 'zones'")
        out["firewall"] = {k: v for k, v in fw.items() if k != "zones"}
    if fs := c.pop("filesystem", None):
        out["filesystem"] = [
            {"mountpoint": f["mountpoint"], "min_size": parse_size(f["minsize"])} for f in fs
        ]
    if dirs := c.pop("directories", None):
        out["directories"] = dirs
    if files := c.pop("files", None):
        for f in files:
            if "uri" in f:
                problems.append(f"file {f['path']}: 'uri'")
        out["files"] = [{k: v for k, v in f.items() if k != "uri"} for f in files]
    if repos := c.pop("repositories", None):
        custom, payload = [], []
        for r in repos:
            install_from = r.get("install_from")
            custom.append(
                _pick(
                    {k: v for k, v in r.items() if k != "install_from"},
                    _REPO_KEYS,
                    f"repository {r['id']}",
                    problems,
                )
            )
            if install_from:
                entry: dict[str, Any] = {"rhsm": False}
                for src, dst in (
                    ("baseurls", "baseurl"),
                    ("metalink", "metalink"),
                    ("mirrorlist", "mirrorlist"),
                ):
                    if r.get(src):
                        entry[dst] = r[src][0] if src == "baseurls" else r[src]
                if r.get("gpgkeys"):
                    entry["gpgkey"] = "\n".join(r["gpgkeys"])
                if "gpgcheck" in r:
                    entry["check_gpg"] = r["gpgcheck"]
                payload.append(entry)
        out["custom_repositories"] = custom
        if payload:
            out["payload_repositories"] = payload
    if "fips" in c:
        out["fips"] = {"enabled": bool(c.pop("fips"))}
    if inst := c.pop("installer", None):
        out["installer"] = _pick(
            inst,
            {"unattended": "unattended", "sudo-nopasswd": "sudo-nopasswd"},
            "installer",
            problems,
        )

    for key, value in c.items():
        if key in _PASSTHROUGH:
            out[key] = value
        else:
            problems.append(f"customizations.{key}")

    if problems and not lossy:
        raise HostedUnsupportedError(problems)
    return out


def to_compose_request(
    bp: Blueprint | Mapping[str, Any],
    image_type: str,
    *,
    distribution: str | None = None,
    architecture: str | None = None,
    upload_type: str = "aws.s3",
    upload_options: Mapping[str, Any] | None = None,
    lossy: bool = False,
) -> dict[str, Any]:
    """A hosted ``ComposeRequest`` for one image of ``image_type``."""
    data = bp.to_dict() if hasattr(bp, "to_dict") else dict(bp)
    distro = distribution or data.get("distro")
    if not distro:
        raise ValueError("the hosted service needs a distribution: set distro= or pass --distro")
    request: dict[str, Any] = {
        "image_name": data["name"],
        "distribution": distro,
        "image_requests": [
            {
                "architecture": architecture or data.get("architecture") or "x86_64",
                "image_type": image_type,
                "upload_request": {"type": upload_type, "options": dict(upload_options or {})},
            }
        ],
        "customizations": to_hosted_customizations(data, lossy=lossy),
    }
    if data.get("description"):
        request["image_description"] = data["description"]
    return request


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def _http(
    method: str,
    url: str,
    *,
    data: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 60,
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method=method, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError as e:
        raise HostedError(f"can't reach {urllib.parse.urlsplit(url).netloc}: {e.reason}") from None


def _error(status: int, body: bytes, what: str) -> HostedError:
    try:
        parsed: Any = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = body[:300].decode(errors="replace")
    detail = parsed
    if isinstance(parsed, dict) and isinstance(parsed.get("errors"), list):
        detail = "; ".join(
            f"{e.get('title', '')}: {e.get('detail', '')}".strip(": ") for e in parsed["errors"]
        )
    elif isinstance(parsed, dict) and "error_description" in parsed:
        detail = parsed["error_description"]
    return HostedError(f"{what} failed (HTTP {status}): {detail}", status=status, body=parsed)


class HostedAuth:
    """Exchanges an offline token or service-account credentials for access tokens."""

    def __init__(
        self,
        *,
        offline_token: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        sso_url: str = DEFAULT_SSO_URL,
        offline_client_id: str = "rhsm-api",
        scope: str = "api.console",
    ) -> None:
        if not offline_token and not (client_id and client_secret):
            raise HostedError(
                "no credentials: set RH_OFFLINE_TOKEN, or RH_CLIENT_ID and RH_CLIENT_SECRET"
            )
        self.offline_token = offline_token
        self.client_id = client_id
        self.client_secret = client_secret
        self.sso_url = sso_url
        self.offline_client_id = offline_client_id
        self.scope = scope
        self._token: str | None = None
        self._expires = 0.0

    @classmethod
    def from_env(cls, **kw: Any) -> HostedAuth:
        return cls(
            offline_token=os.environ.get("RH_OFFLINE_TOKEN"),
            client_id=os.environ.get("RH_CLIENT_ID"),
            client_secret=os.environ.get("RH_CLIENT_SECRET"),
            **kw,
        )

    def token(self) -> str:
        if self._token and time.monotonic() < self._expires - 30:
            return self._token
        if self.client_id and self.client_secret:
            form = {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": self.scope,
            }
        else:
            form = {
                "grant_type": "refresh_token",
                "client_id": self.offline_client_id,
                "refresh_token": str(self.offline_token),
            }
        status, body = _http(
            "POST",
            self.sso_url,
            data=urllib.parse.urlencode(form).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if status != 200:
            raise _error(status, body, "SSO token request")
        reply = json.loads(body)
        self._token = reply["access_token"]
        self._expires = time.monotonic() + float(reply.get("expires_in", 300))
        return self._token


class HostedClient:
    """A small client for the hosted image-builder API."""

    def __init__(self, auth: HostedAuth, *, api_url: str = DEFAULT_API_URL, timeout: float = 60):
        self.auth = auth
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout

    @classmethod
    def from_env(cls, **kw: Any) -> HostedClient:
        api_url = os.environ.get("BPKIT_HOSTED_API_URL", DEFAULT_API_URL)
        auth_kw = {}
        if sso := os.environ.get("BPKIT_HOSTED_SSO_URL"):
            auth_kw["sso_url"] = sso
        return cls(HostedAuth.from_env(**auth_kw), api_url=api_url, **kw)

    def _call(self, method: str, path: str, payload: Any = None, *, what: str) -> Any:
        headers = {"Authorization": f"Bearer {self.auth.token()}", "Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        status, body = _http(
            method, self.api_url + path, data=data, headers=headers, timeout=self.timeout
        )
        if status >= 400:
            raise _error(status, body, what)
        return json.loads(body) if body else None

    # -- composes -----------------------------------------------------------

    def compose(self, bp: Blueprint | Mapping[str, Any], image_type: str, **kw: Any) -> str:
        """Start a compose from a blueprint. Returns the compose id.

        ``kw`` goes to :func:`to_compose_request` (``distribution``,
        ``architecture``, ``upload_type``, ``upload_options``, ``lossy``).
        """
        request = to_compose_request(bp, image_type, **kw)
        return str(self._call("POST", "/compose", request, what="compose")["id"])

    def compose_status(self, compose_id: str) -> dict[str, Any]:
        reply = self._call("GET", f"/composes/{compose_id}", what="compose status")
        return dict(reply["image_status"])

    def wait(
        self,
        compose_id: str,
        *,
        poll: float = 30,
        timeout: float | None = None,
        on_status: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Poll until the compose succeeds; raises ``HostedError`` on failure."""
        deadline = None if timeout is None else time.monotonic() + timeout
        last = None
        while True:
            status = self.compose_status(compose_id)
            state = status.get("status")
            if on_status is not None and state != last:
                on_status(status)
            last = state
            if state == "success":
                return status
            if state == "failure":
                err = status.get("error") or {}
                raise HostedError(f"compose {compose_id} failed: {err.get('reason', 'unknown')}")
            if deadline is not None and time.monotonic() > deadline:
                raise HostedError(f"timed out waiting for compose {compose_id} ({state})")
            time.sleep(poll)

    def download(self, compose_id: str, dest: str | Path = ".") -> Path:
        """Download a finished ``aws.s3`` compose into ``dest`` (dir or file)."""
        status = self.compose_status(compose_id)
        upload = status.get("upload_status") or {}
        url = (upload.get("options") or {}).get("url")
        if status.get("status") != "success" or not url:
            raise HostedError(
                f"compose {compose_id} has no download URL "
                f"(status {status.get('status')!r}, upload type {upload.get('type')!r})"
            )
        dest = Path(dest)
        name = Path(urllib.parse.urlsplit(url).path).name or f"{compose_id}.img"
        final = dest / name if dest.is_dir() else dest
        tmp = final.with_name(f".{final.name}.part")
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp, open(tmp, "wb") as f:
                while chunk := resp.read(1 << 20):
                    f.write(chunk)
        except urllib.error.URLError as e:
            tmp.unlink(missing_ok=True)
            raise HostedError(f"download failed: {e}") from None
        tmp.replace(final)
        return final

    # -- stored blueprints --------------------------------------------------

    def find_blueprint(self, name: str) -> str | None:
        q = urllib.parse.urlencode({"name": name})
        reply = self._call("GET", f"/blueprints?{q}", what="blueprint lookup")
        data = reply.get("data", []) if reply else []
        return str(data[0]["id"]) if data else None

    def push(
        self,
        bp: Blueprint | Mapping[str, Any],
        image_types: list[str],
        **kw: Any,
    ) -> str:
        """Create or update a stored blueprint (matched by name). Returns its id."""
        if not image_types:
            raise ValueError("hosted blueprints need at least one image type")
        requests = [to_compose_request(bp, t, **kw) for t in image_types]
        first = requests[0]
        body: dict[str, Any] = {
            "name": first["image_name"],
            "description": first.get("image_description", ""),
            "distribution": first["distribution"],
            "image_requests": [r["image_requests"][0] for r in requests],
            "customizations": first["customizations"],
        }
        existing = self.find_blueprint(body["name"])
        if existing:
            self._call("PUT", f"/blueprints/{existing}", body, what="blueprint update")
            return existing
        return str(self._call("POST", "/blueprints", body, what="blueprint create")["id"])

    def compose_blueprint(
        self, blueprint_id: str, image_types: list[str] | None = None
    ) -> list[str]:
        payload = {"image_types": image_types} if image_types else {}
        reply = self._call("POST", f"/blueprints/{blueprint_id}/compose", payload, what="compose")
        return [str(r["id"]) for r in reply]
