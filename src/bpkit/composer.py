"""Client for osbuild-composer's Weldr API (what ``composer-cli`` talks to).

The API listens on a unix socket, ``/run/weldr/api.socket`` by default. You
need to be root or in the ``weldr`` group to use it::

    from bpkit.composer import ComposerClient

    client = ComposerClient()
    client.push(WebServer())                      # POST /blueprints/new
    build = client.compose("web-server", "qcow2") # POST /compose
    status = client.wait(build)                   # poll /compose/status
    path = client.download(build, "out/")         # GET /compose/image

Only the standard library is used.
"""

from __future__ import annotations

import http.client
import json
import socket
import time
import urllib.parse
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .render import to_toml

if TYPE_CHECKING:
    from .blueprint import Blueprint

__all__ = ["DEFAULT_SOCKET", "ComposerClient", "ComposerError"]

DEFAULT_SOCKET = "/run/weldr/api.socket"
#: Compose states reported by ``compose/status``.
FINISHED, FAILED = "FINISHED", "FAILED"


class ComposerError(RuntimeError):
    """osbuild-composer returned an error (``{"status": false, "errors": [...]}``)."""

    def __init__(self, message: str, *, status: int | None = None, errors: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.errors = errors or []


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


def blueprint_toml(blueprint: Blueprint | Mapping[str, Any] | str) -> tuple[str, str]:
    """(name, TOML text) for a Blueprint instance or class, a dict, or TOML text."""
    import tomllib

    from .blueprint import Blueprint

    if isinstance(blueprint, type) and issubclass(blueprint, Blueprint):
        blueprint = blueprint()
    if isinstance(blueprint, Blueprint):
        return blueprint.name, blueprint.to_toml()
    if isinstance(blueprint, Mapping):
        return str(blueprint["name"]), to_toml(dict(blueprint))
    if isinstance(blueprint, str):
        return str(tomllib.loads(blueprint)["name"]), blueprint
    raise TypeError(f"expected a Blueprint, dict or TOML string, got {type(blueprint).__name__}")


class ComposerClient:
    """A small, synchronous Weldr API client."""

    def __init__(
        self, socket_path: str | Path = DEFAULT_SOCKET, *, api_version: int = 1, timeout: float = 60
    ) -> None:
        self.socket_path = str(socket_path)
        self.api_version = api_version
        self.timeout = timeout

    # -- transport ----------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        stream_to: Path | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        conn = _UnixHTTPConnection(self.socket_path, self.timeout)
        headers = {"Content-Type": content_type} if content_type else {}
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            if stream_to is not None and resp.status == 200:
                with open(stream_to, "wb") as f:
                    while chunk := resp.read(1 << 20):
                        f.write(chunk)
                return resp.status, resp_headers, b""
            return resp.status, resp_headers, resp.read()
        except FileNotFoundError:
            raise ComposerError(
                f"osbuild-composer socket {self.socket_path} not found; is "
                "osbuild-composer.socket running? (systemctl enable --now osbuild-composer.socket)"
            ) from None
        except PermissionError:
            raise ComposerError(
                f"permission denied on {self.socket_path}; run as root or join the 'weldr' group"
            ) from None
        finally:
            conn.close()

    def _api(self, path: str) -> str:
        return f"/api/v{self.api_version}/{path.lstrip('/')}"

    def _json(self, method: str, path: str, payload: Any = None, **kw: Any) -> Any:
        body = None if payload is None else json.dumps(payload).encode()
        status, _, data = self._request(
            method, path, body=body, content_type="application/json" if body else None, **kw
        )
        return self._decode(status, data)

    @staticmethod
    def _decode(status: int, data: bytes) -> Any:
        try:
            reply = json.loads(data) if data else {}
        except json.JSONDecodeError:
            if status >= 400:
                raise ComposerError(f"HTTP {status}: {data[:200]!r}", status=status) from None
            return data.decode()
        if status >= 400 or (isinstance(reply, dict) and reply.get("status") is False):
            errors = reply.get("errors", []) if isinstance(reply, dict) else []
            msg = "; ".join(f"{e.get('id', '?')}: {e.get('msg', '')}" for e in errors) or str(reply)
            raise ComposerError(msg, status=status, errors=errors)
        return reply

    # -- API ----------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Server info (``/api/status``)."""
        return self._json("GET", "/api/status")

    def push(self, blueprint: Blueprint | Mapping[str, Any] | str) -> str:
        """Upload (create or replace) a blueprint. Returns its name."""
        name, text = blueprint_toml(blueprint)
        status, _, data = self._request(
            "POST", self._api("blueprints/new"), body=text.encode(), content_type="text/x-toml"
        )
        self._decode(status, data)
        return name

    def get_blueprint(self, name: str) -> str:
        """A stored blueprint as TOML text."""
        q = urllib.parse.quote(name, safe="")
        status, _, data = self._request("GET", self._api(f"blueprints/info/{q}?format=toml"))
        if status >= 400:
            self._decode(status, data)
        return data.decode()

    def list_blueprints(self) -> list[str]:
        return list(
            self._json("GET", self._api("blueprints/list?limit=1000")).get("blueprints", [])
        )

    def delete_blueprint(self, name: str) -> None:
        self._json("DELETE", self._api(f"blueprints/delete/{urllib.parse.quote(name, safe='')}"))

    def depsolve(self, name: str) -> dict[str, Any]:
        q = urllib.parse.quote(name, safe="")
        return self._json("GET", self._api(f"blueprints/depsolve/{q}"))

    def compose_types(self) -> list[str]:
        reply = self._json("GET", self._api("compose/types"))
        return [t["name"] for t in reply.get("types", []) if t.get("enabled", True)]

    def compose(
        self,
        blueprint_name: str,
        image_type: str,
        *,
        size: int | None = None,
        branch: str | None = None,
        upload: Mapping[str, Any] | None = None,
    ) -> str:
        """Start a compose. Returns its build UUID."""
        payload: dict[str, Any] = {"blueprint_name": blueprint_name, "compose_type": image_type}
        if branch:
            payload["branch"] = branch
        if size:
            payload["size"] = size
        if upload:
            payload["upload"] = dict(upload)
        reply = self._json("POST", self._api("compose"), payload)
        return str(reply["build_id"])

    def compose_status(self, build_id: str) -> dict[str, Any]:
        """The compose entry: ``queue_status`` is WAITING, RUNNING, FINISHED or FAILED."""
        reply = self._json("GET", self._api(f"compose/status/{build_id}"))
        entries = reply.get("uuids", [])
        if not entries:
            raise ComposerError(f"unknown compose {build_id}")
        return dict(entries[0])

    def wait(
        self,
        build_id: str,
        *,
        poll: float = 10,
        timeout: float | None = None,
        on_status: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Poll until the compose finishes. Raises ``ComposerError`` if it failed."""
        deadline = None if timeout is None else time.monotonic() + timeout
        last = None
        while True:
            entry = self.compose_status(build_id)
            state = entry.get("queue_status")
            if on_status is not None and state != last:
                on_status(entry)
            last = state
            if state == FINISHED:
                return entry
            if state == FAILED:
                raise ComposerError(
                    f"compose {build_id} failed (see `composer-cli compose log {build_id}` "
                    "or ComposerClient.log())"
                )
            if deadline is not None and time.monotonic() > deadline:
                raise ComposerError(f"timed out waiting for compose {build_id} ({state})")
            time.sleep(poll)

    def download(self, build_id: str, dest: str | Path = ".") -> Path:
        """Download the finished image into ``dest`` (a directory or a file path)."""
        dest = Path(dest)
        tmp = (dest if dest.is_dir() else dest.parent) / f".{build_id}.part"
        status, headers, data = self._request(
            "GET", self._api(f"compose/image/{build_id}"), stream_to=tmp
        )
        if status != 200:
            tmp.unlink(missing_ok=True)
            self._decode(status, data)
        filename = _disposition_filename(headers.get("content-disposition")) or f"{build_id}.img"
        final = dest / filename if dest.is_dir() else dest
        tmp.replace(final)
        return final

    def log(self, build_id: str) -> str:
        """The osbuild log of a compose (text)."""
        status, _, data = self._request("GET", self._api(f"compose/log/{build_id}"))
        if status >= 400:
            self._decode(status, data)
        return data.decode(errors="replace")

    def cancel(self, build_id: str) -> None:
        self._json("DELETE", self._api(f"compose/cancel/{build_id}"))

    def delete(self, build_id: str) -> None:
        self._json("DELETE", self._api(f"compose/delete/{build_id}"))


def _disposition_filename(value: str | None) -> str | None:
    if not value:
        return None
    for part in value.split(";"):
        key, _, val = part.strip().partition("=")
        if key.lower() == "filename" and val:
            name = Path(val.strip('"')).name
            return name or None
    return None
