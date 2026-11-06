"""Fakes for osbuild-composer, the hosted service, and image-builder."""

import json
import os
import socketserver
import stat
import sys
import threading
import tomllib
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest


class _Quiet(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def address_string(self):
        return "test"

    def reply(self, status, body, ctype="application/json", headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n)


# -- osbuild-composer (Weldr over a unix socket) ------------------------------


class FakeComposer:
    def __init__(self):
        self.blueprints: dict[str, str] = {}
        self.composes: dict[str, dict] = {}
        self.polls_until_done = 2
        self.fail = False


def _weldr_handler(state: FakeComposer):
    class Handler(_Quiet):
        def error(self, status, msg, id_="BadRequest"):
            self.reply(status, {"status": False, "errors": [{"id": id_, "msg": msg}]})

        def do_GET(self):
            path, _, query = self.path.partition("?")
            parts = path.split("/")[3:]
            if path == "/api/status":
                return self.reply(200, {"api": "1", "backend": "osbuild-composer"})
            if parts[:2] == ["blueprints", "info"]:
                name = urllib.parse.unquote(parts[2])
                if name not in state.blueprints:
                    return self.error(400, f"{name}: blueprint not found", "UnknownBlueprint")
                assert "format=toml" in query
                return self.reply(200, state.blueprints[name].encode(), "text/x-toml")
            if parts[:2] == ["compose", "types"]:
                return self.reply(
                    200,
                    {
                        "types": [
                            {"name": "qcow2", "enabled": True},
                            {"name": "ami", "enabled": False},
                        ]
                    },
                )
            if parts[:2] == ["compose", "status"]:
                c = state.composes.get(parts[2])
                if c is None:
                    return self.reply(200, {"uuids": []})
                c["polls"] += 1
                if c["polls"] >= state.polls_until_done:
                    c["queue_status"] = "FAILED" if state.fail else "FINISHED"
                else:
                    c["queue_status"] = "RUNNING"
                entry = {k: v for k, v in c.items() if k != "polls"}
                return self.reply(200, {"uuids": [entry]})
            if parts[:2] == ["compose", "image"]:
                return self.reply(
                    200,
                    b"QCOW2-BYTES",
                    "application/x-qemu-disk",
                    {"Content-Disposition": f"attachment; filename={parts[2]}-disk.qcow2"},
                )
            if parts[:2] == ["compose", "log"]:
                return self.reply(200, b"osbuild log", "text/plain")
            return self.error(404, f"no route {path}")

        def do_POST(self):
            path = self.path.split("?")[0]
            body = self.body()
            if path == "/api/v1/blueprints/new":
                if self.headers["Content-Type"] != "text/x-toml":
                    return self.error(400, "bad content type")
                bp = tomllib.loads(body.decode())
                state.blueprints[bp["name"]] = body.decode()
                return self.reply(200, {"status": True})
            if path == "/api/v1/compose":
                req = json.loads(body)
                if req["blueprint_name"] not in state.blueprints:
                    return self.error(
                        400, f"Unknown blueprint name: {req['blueprint_name']}", "UnknownBlueprint"
                    )
                build_id = str(uuid.uuid4())
                state.composes[build_id] = {
                    "id": build_id,
                    "blueprint": req["blueprint_name"],
                    "compose_type": req["compose_type"],
                    "queue_status": "WAITING",
                    "polls": 0,
                }
                return self.reply(200, {"build_id": build_id, "status": True, "warnings": []})
            return self.error(404, f"no route {path}")

    return Handler


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


@pytest.fixture
def composer(tmp_path):
    state = FakeComposer()
    sock = str(tmp_path / "api.socket")
    server = _UnixServer(sock, _weldr_handler(state))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    state.socket = sock
    yield state
    server.shutdown()
    server.server_close()


# -- hosted service (SSO + API over HTTP on localhost) -----------------------


class FakeHosted:
    def __init__(self):
        self.tokens_issued = 0
        self.token_requests: list[dict] = []
        self.requests: list[tuple[str, str, object]] = []
        self.composes: dict[str, dict] = {}
        self.blueprints: dict[str, dict] = {}
        self.polls_until_done = 2
        self.fail = False
        self.base = ""


def _hosted_handler(state: FakeHosted):
    class Handler(_Quiet):
        def authed(self):
            return self.headers.get("Authorization", "").startswith("Bearer tok-")

        def do_POST(self):
            body = self.body()
            if self.path == "/sso/token":
                form = dict(urllib.parse.parse_qsl(body.decode()))
                state.token_requests.append(form)
                if form.get("refresh_token") == "bad" or form.get("client_secret") == "bad":
                    return self.reply(
                        400,
                        {"error": "invalid_grant", "error_description": "Invalid refresh token"},
                    )
                state.tokens_issued += 1
                return self.reply(
                    200, {"access_token": f"tok-{state.tokens_issued}", "expires_in": 900}
                )
            if not self.authed():
                return self.reply(401, {"errors": [{"title": "401", "detail": "no token"}]})
            payload = json.loads(body) if body else None
            state.requests.append(("POST", self.path, payload))
            if self.path == "/api/compose":
                cid = str(uuid.uuid4())
                state.composes[cid] = {"polls": 0, "request": payload}
                return self.reply(201, {"id": cid})
            if self.path == "/api/blueprints":
                bid = str(uuid.uuid4())
                state.blueprints[bid] = payload
                return self.reply(201, {"id": bid})
            if self.path.startswith("/api/blueprints/") and self.path.endswith("/compose"):
                return self.reply(201, [{"id": str(uuid.uuid4())}])
            return self.reply(404, {"errors": [{"title": "404", "detail": self.path}]})

        def do_PUT(self):
            payload = json.loads(self.body())
            state.requests.append(("PUT", self.path, payload))
            bid = self.path.rsplit("/", 1)[1]
            state.blueprints[bid] = payload
            return self.reply(200, {"id": bid})

        def do_GET(self):
            if self.path.startswith("/files/"):
                return self.reply(200, b"IMAGE", "application/octet-stream")
            if not self.authed():
                return self.reply(401, {"errors": [{"title": "401", "detail": "no token"}]})
            path, _, query = self.path.partition("?")
            if path == "/api/blueprints":
                name = dict(urllib.parse.parse_qsl(query)).get("name")
                data = [
                    {
                        "id": k,
                        "name": v["name"],
                        "version": 1,
                        "description": "",
                        "last_modified_at": "",
                    }
                    for k, v in state.blueprints.items()
                    if v["name"] == name
                ]
                return self.reply(200, {"meta": {"count": len(data)}, "links": {}, "data": data})
            if path.startswith("/api/composes/"):
                cid = path.rsplit("/", 1)[1]
                c = state.composes.get(cid)
                if c is None:
                    return self.reply(404, {"errors": [{"title": "404", "detail": "no compose"}]})
                c["polls"] += 1
                if c["polls"] < state.polls_until_done:
                    status = {"status": "building"}
                elif state.fail:
                    status = {"status": "failure", "error": {"id": 1, "reason": "depsolve failed"}}
                else:
                    status = {
                        "status": "success",
                        "upload_status": {
                            "status": "success",
                            "type": "aws.s3",
                            "options": {"url": f"{state.base}/files/composer-api-{cid}-disk.qcow2"},
                        },
                    }
                return self.reply(200, {"image_status": status, "request": c["request"]})
            return self.reply(404, {"errors": [{"title": "404", "detail": path}]})

    return Handler


@pytest.fixture
def hosted(monkeypatch):
    state = FakeHosted()
    server = HTTPServer(("127.0.0.1", 0), _hosted_handler(state))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{server.server_port}"
    state.base = base
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(var, "127.0.0.1,localhost")
    monkeypatch.setenv("BPKIT_HOSTED_API_URL", base + "/api")
    monkeypatch.setenv("BPKIT_HOSTED_SSO_URL", base + "/sso/token")
    monkeypatch.setenv("RH_OFFLINE_TOKEN", "offline-123")
    monkeypatch.delenv("RH_CLIENT_ID", raising=False)
    monkeypatch.delenv("RH_CLIENT_SECRET", raising=False)
    yield state
    server.shutdown()
    server.server_close()


# -- image-builder CLI ---------------------------------------------------------


FAKE_IMAGE_BUILDER = """#!{python}
import json, sys, pathlib
args = sys.argv[1:]
log = pathlib.Path({log!r})
bp = args[args.index("--blueprint") + 1]
log.write_text(json.dumps({{"argv": args, "blueprint": pathlib.Path(bp).read_text()}}))
if "FAIL" in pathlib.Path(bp).read_text():
    print("depsolve failed", file=sys.stderr)
    sys.exit(3)
if args[0] == "manifest":
    print(json.dumps({{"version": "2", "pipelines": []}}))
else:
    out = pathlib.Path(args[args.index("--output-dir") + 1])
    (out / (args[1] + ".img")).write_text("image")
"""


@pytest.fixture
def fake_image_builder(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "ib-call.json"
    exe = bindir / "image-builder"
    exe.write_text(FAKE_IMAGE_BUILDER.format(python=sys.executable, log=str(log)))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("BPKIT_IMAGE_BUILDER", raising=False)

    def last_call():
        return json.loads(log.read_text())

    return last_call


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).resolve().parent
