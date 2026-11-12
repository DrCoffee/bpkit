"""osbuild-composer, hosted service and local image-builder backends (against fakes)."""

import json
import tomllib

import pytest

from bpkit import Blueprint, Container, File, Filesystem, Firewall, Repository, Services, User
from bpkit.build import BuildError, build, image_builder_command, manifest
from bpkit.cli import main
from bpkit.composer import ComposerClient, ComposerError
from bpkit.hosted import (
    HostedAuth,
    HostedClient,
    HostedError,
    HostedUnsupportedError,
    to_compose_request,
    to_hosted_customizations,
)


class Web(Blueprint):
    """Web box."""

    distro = "rhel-9"
    packages = ["nginx", "vim"]
    services = Services(enabled=["nginx"])
    users = [User("ops", groups=["wheel"], key="ssh-ed25519 AAA")]


FLEET_TARGET = "examples/fleet.py:WebServer"


# -- osbuild-composer --------------------------------------------------------


def test_composer_push_compose_wait_download(composer, tmp_path):
    client = ComposerClient(composer.socket)
    assert client.status()["backend"] == "osbuild-composer"
    assert client.push(Web()) == "web"
    assert tomllib.loads(client.get_blueprint("web")) == Web().to_dict()
    assert client.compose_types() == ["qcow2"]

    build_id = client.compose("web", "qcow2")
    seen = []
    entry = client.wait(build_id, poll=0, on_status=lambda e: seen.append(e["queue_status"]))
    assert entry["queue_status"] == "FINISHED"
    assert seen == ["RUNNING", "FINISHED"]
    path = client.download(build_id, tmp_path)
    assert path.name == f"{build_id}-disk.qcow2"
    assert path.read_bytes() == b"QCOW2-BYTES"
    assert client.log(build_id) == "osbuild log"


def test_composer_errors(composer, tmp_path):
    client = ComposerClient(composer.socket)
    with pytest.raises(ComposerError, match="UnknownBlueprint: Unknown blueprint name: nope"):
        client.compose("nope", "qcow2")
    with pytest.raises(ComposerError, match="unknown compose"):
        client.compose_status("00000000-0000-0000-0000-000000000000")
    composer.fail = True
    client.push(Web())
    bid = client.compose("web", "qcow2")
    with pytest.raises(ComposerError, match="failed"):
        client.wait(bid, poll=0)
    with pytest.raises(ComposerError, match=r"not found; is osbuild-composer\.socket running"):
        ComposerClient(tmp_path / "missing.socket").status()


def test_composer_cli_build_and_import(composer, tmp_path, capsys):
    out = tmp_path / "out"
    rc = main(
        [
            "build",
            FLEET_TARGET,
            "-t",
            "qcow2",
            "--backend",
            "composer",
            "--socket",
            composer.socket,
            "-o",
            str(out),
            "--poll",
            "0",
        ]
    )
    assert rc == 0, capsys.readouterr().err
    assert len(list(out.iterdir())) == 1
    assert "web-server" in composer.blueprints

    module = tmp_path / "from_composer.py"
    assert (
        main(
            [
                "import",
                "--from-composer",
                "web-server",
                "--socket",
                composer.socket,
                "-o",
                str(module),
            ]
        )
        == 0
    )
    assert "class WebServer(Blueprint)" in module.read_text()

    assert main(["push", "examples/fleet.py:MinimalWeb", "--socket", composer.socket]) == 0
    assert "minimal-web" in composer.blueprints


# -- hosted: conversion ------------------------------------------------------


def test_hosted_conversion():
    class Full(Blueprint):
        distro = "rhel-9"
        packages = ["vim", ("nginx")]
        modules = ["nodejs"]
        groups = ["Development Tools"]
        enabled_modules = ["nodejs:20"]
        containers = [Container("quay.io/x:1", tls_verify=False)]
        users = [User("ops", groups=["wheel"], key="k1", password="$6$x")]
        sshkeys = [{"user": "root", "key": "k2"}]
        filesystem = [Filesystem("/var", "1 GiB")]
        firewall = Firewall(ports=["22:tcp"])
        files = [File("/etc/motd", "hi\n", mode="0644")]
        repositories = [Repository("r", ["https://r/x"], gpgcheck=True, install_from=True)]
        fips = True
        kernel_append = ["audit=1"]
        timezone = "UTC"
        hostname = "h"
        extra_customizations = {"openscap": {"profile_id": "cis"}}

    c = to_hosted_customizations(Full().to_dict())
    assert c["packages"] == ["vim", "nginx", "nodejs", "@Development Tools"]
    assert c["enabled_modules"] == [{"name": "nodejs", "stream": "20"}]
    assert c["containers"] == [{"source": "quay.io/x:1", "tls_verify": False}]
    assert c["users"] == [
        {"name": "ops", "groups": ["wheel"], "ssh_key": "k1", "password": "$6$x"},
        {"name": "root", "ssh_key": "k2"},
    ]
    assert c["filesystem"] == [{"mountpoint": "/var", "min_size": 1024**3}]
    assert c["fips"] == {"enabled": True}
    assert c["kernel"] == {"append": "audit=1"}
    assert c["timezone"] == {"timezone": "UTC"}
    assert c["custom_repositories"] == [{"id": "r", "baseurl": ["https://r/x"], "check_gpg": True}]
    assert c["payload_repositories"] == [
        {"rhsm": False, "baseurl": "https://r/x", "check_gpg": True}
    ]
    assert c["openscap"] == {"profile_id": "cis"} and c["hostname"] == "h"
    assert c["files"] == [{"path": "/etc/motd", "data": "hi\n", "mode": "0644"}]


def test_hosted_conversion_refuses_unsupported():
    class Odd(Blueprint):
        distro = "rhel-9"
        users = [User("ops", shell="/bin/zsh", uid=1000)]
        services = Services(default_target="graphical.target")
        extra_customizations = {"rpm": {"import_keys": {"files": ["/k"]}}}

    with pytest.raises(HostedUnsupportedError) as exc:
        to_hosted_customizations(Odd().to_dict())
    assert exc.value.problems == [
        "user ops: 'shell'",
        "user ops: 'uid'",
        "services: 'default_target'",
        "customizations.rpm",
    ]
    lossy = to_hosted_customizations(Odd().to_dict(), lossy=True)
    assert lossy["users"] == [{"name": "ops"}]


def test_compose_request_shape():
    req = to_compose_request(Web(), "guest-image")
    assert req == {
        "image_name": "web",
        "image_description": "Web box.",
        "distribution": "rhel-9",
        "image_requests": [
            {
                "architecture": "x86_64",
                "image_type": "guest-image",
                "upload_request": {"type": "aws.s3", "options": {}},
            }
        ],
        "customizations": {
            "packages": ["nginx", "vim"],
            "users": [{"name": "ops", "groups": ["wheel"], "ssh_key": "ssh-ed25519 AAA"}],
            "services": {"enabled": ["nginx"]},
        },
    }

    class NoDistro(Blueprint):
        pass

    with pytest.raises(ValueError, match="needs a distribution"):
        to_compose_request(NoDistro(), "guest-image")


# -- hosted: client ----------------------------------------------------------


def test_hosted_compose_wait_download(hosted, tmp_path):
    client = HostedClient.from_env()
    cid = client.compose(Web(), "guest-image")
    method, path, payload = hosted.requests[-1]
    assert (method, path) == ("POST", "/api/compose")
    assert payload["customizations"]["packages"] == ["nginx", "vim"]
    status = client.wait(cid, poll=0)
    assert status["status"] == "success"
    out = client.download(cid, tmp_path)
    assert out.read_bytes() == b"IMAGE"
    assert hosted.tokens_issued == 1  # access token cached across calls
    assert hosted.token_requests[0] == {
        "grant_type": "refresh_token",
        "client_id": "rhsm-api",
        "refresh_token": "offline-123",
    }


def test_hosted_failure_and_auth_errors(hosted, monkeypatch):
    hosted.fail = True
    client = HostedClient.from_env()
    cid = client.compose(Web(), "guest-image")
    with pytest.raises(HostedError, match="depsolve failed"):
        client.wait(cid, poll=0)
    monkeypatch.setenv("RH_OFFLINE_TOKEN", "bad")
    with pytest.raises(HostedError, match="Invalid refresh token"):
        HostedClient.from_env().compose(Web(), "guest-image")
    monkeypatch.delenv("RH_OFFLINE_TOKEN")
    with pytest.raises(HostedError, match="no credentials"):
        HostedClient.from_env()


def test_hosted_service_account(hosted, monkeypatch):
    monkeypatch.delenv("RH_OFFLINE_TOKEN")
    monkeypatch.setenv("RH_CLIENT_ID", "sa-id")
    monkeypatch.setenv("RH_CLIENT_SECRET", "sa-secret")
    HostedClient.from_env().compose(Web(), "guest-image")
    assert hosted.token_requests[0]["grant_type"] == "client_credentials"
    assert hosted.token_requests[0]["scope"] == "api.console"


def test_hosted_push_creates_then_updates(hosted):
    client = HostedClient.from_env()
    bid = client.push(Web(), ["guest-image", "aws"])
    assert hosted.blueprints[bid]["name"] == "web"
    assert [r["image_type"] for r in hosted.blueprints[bid]["image_requests"]] == [
        "guest-image",
        "aws",
    ]
    assert client.push(Web(), ["guest-image"]) == bid
    assert hosted.requests[-1][0] == "PUT"
    assert len(client.compose_blueprint(bid)) == 1


def test_hosted_cli(hosted, tmp_path, capsys):
    out = tmp_path / "o"
    rc = main(
        [
            "build",
            FLEET_TARGET,
            "-t",
            "guest-image",
            "--backend",
            "hosted",
            "--lossy",
            "-o",
            str(out),
            "--poll",
            "0",
        ]
    )
    assert rc == 0, capsys.readouterr().err
    assert len(list(out.iterdir())) == 1
    cid = capsys.readouterr().out.strip().splitlines()[0]
    assert main(["status", cid, "--backend", "hosted"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "success"
    # without --lossy, unsupported settings are reported, not dropped
    assert (
        main(
            [
                "build",
                "examples/fleet.py:StagingWeb",
                "-t",
                "guest-image",
                "--backend",
                "hosted",
                "--no-wait",
            ]
        )
        == 2
    )
    assert "user ops: 'shell'" in capsys.readouterr().err


def test_hosted_auth_requires_credentials():
    with pytest.raises(HostedError):
        HostedAuth()


# -- local image-builder -----------------------------------------------------


def test_image_builder_command():
    assert image_builder_command(
        "build",
        "qcow2",
        "/t/bp.toml",
        distro="rhel-9",
        arch="aarch64",
        output_dir="out",
        extra_args=["--with-manifest"],
    ) == [
        "image-builder",
        "build",
        "qcow2",
        "--blueprint",
        "/t/bp.toml",
        "--distro",
        "rhel-9",
        "--arch",
        "aarch64",
        "--output-dir",
        "out",
        "--with-manifest",
    ]


def test_string_build_repos_is_one_url(fake_image_builder, tmp_path):
    class Mirror(Web):
        BUILD_REPOS = "http://mirror.lan/os/"

    build(Mirror(), "qcow2", output_dir=tmp_path / "out", extra_repos="file:///srv/extra/")
    argv = fake_image_builder()["argv"]
    assert argv[argv.index("--force-repo") + 1] == "http://mirror.lan/os/"
    assert argv.count("--force-repo") == argv.count("--extra-repo") == 1


def test_local_build(fake_image_builder, tmp_path):
    out = build(Web(), "qcow2", output_dir=tmp_path / "out")
    call = fake_image_builder()
    assert call["argv"][:2] == ["build", "qcow2"]
    assert call["argv"][call["argv"].index("--distro") + 1] == "rhel-9"  # from the blueprint
    assert tomllib.loads(call["blueprint"]) == Web().to_dict()
    assert (out / "qcow2.img").exists()
    assert json.loads(manifest(Web(), "qcow2"))["version"] == "2"


def test_local_build_failure(fake_image_builder, tmp_path):
    class Bad(Blueprint):
        hostname = "FAIL"

    with pytest.raises(BuildError, match="exit 3"):
        build(Bad(), "qcow2", output_dir=tmp_path, capture=True)
    with pytest.raises(BuildError, match="depsolve failed"):
        manifest(Bad(), "qcow2")


def test_local_build_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(BuildError, match="not found on PATH"):
        build(Web(), "qcow2", output_dir=tmp_path)


def test_local_build_cli(fake_image_builder, tmp_path, capsys):
    rc = main(
        [
            "build",
            FLEET_TARGET,
            "-t",
            "qcow2",
            "-o",
            str(tmp_path),
            "--distro",
            "centos-9",
            "--",
            "--with-manifest",
        ]
    )
    assert rc == 0, capsys.readouterr().err
    argv = fake_image_builder()["argv"]
    assert argv[argv.index("--distro") + 1] == "centos-9"
    assert argv[-1] == "--with-manifest"
