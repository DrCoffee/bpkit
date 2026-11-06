"""Model shorthands, validation and TOML-name mapping."""

import pytest
from pydantic import ValidationError

from bpkit import (
    Container,
    Directory,
    EnabledModule,
    File,
    Filesystem,
    Group,
    Installer,
    Package,
    Repository,
    Services,
    SSHKey,
    User,
)
from bpkit.models import parse_size


def test_package_positional_and_default_version():
    assert Package("vim").to_blueprint() == {"name": "vim", "version": "*"}
    assert Package("vim", "9.1*").version == "9.1*"
    with pytest.raises(TypeError, match="at most 2 positional"):
        Package("a", "b", "c")
    with pytest.raises(TypeError, match="multiple values"):
        Package("a", name="b")


def test_enabled_module_shorthand():
    assert EnabledModule.from_shorthand("nodejs:20").to_blueprint() == {
        "name": "nodejs",
        "stream": "20",
    }


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ("10 GiB", 10 * 1024**3),
        ("512MiB", 512 * 1024**2),
        ("1.5 GB", 1_500_000_000),
        ("4096", 4096),
        (2048, 2048),
    ],
)
def test_parse_size(size, expected):
    assert parse_size(size) == expected


@pytest.mark.parametrize("size", ["10 gib", "ten GiB", "-1 GiB", 0, "1 PB"])
def test_parse_size_rejects(size):
    with pytest.raises(ValueError):
        parse_size(size)


def test_filesystem():
    fs = Filesystem("/var", "10 GiB")
    assert fs.to_blueprint() == {"mountpoint": "/var", "minsize": "10 GiB"}
    assert fs.size_bytes == 10 * 1024**3
    assert Filesystem(mountpoint="/home", size=1024).minsize == 1024
    for bad in ["var", "/var/", "/var/../etc"]:
        with pytest.raises(ValidationError):
            Filesystem(bad, "1 GiB")
    with pytest.raises(ValidationError):
        Filesystem("/var", "lots")


def test_user_fields():
    u = User(
        "ops",
        description="Ops",
        key="ssh-ed25519 AAA",
        groups="wheel",
        uid=1000,
        force_password_reset=True,
    )
    assert u.to_blueprint() == {
        "name": "ops",
        "description": "Ops",
        "key": "ssh-ed25519 AAA",
        "groups": ["wheel"],
        "uid": 1000,
        "force_password_reset": True,
    }
    with pytest.raises(ValidationError):
        User("x", nonsense=1)


def test_container_aliases():
    c = Container("quay.io/x:1", tls_verify=False, local_storage=True)
    assert c.to_blueprint() == {"source": "quay.io/x:1", "tls-verify": False, "local-storage": True}
    assert Container.model_validate({"source": "a", "tls-verify": True}).tls_verify is True


def test_file_and_directory():
    f = File("/etc/motd", "hi\n", mode=0o644, user="root", group=0)
    assert f.to_blueprint() == {
        "path": "/etc/motd",
        "data": "hi\n",
        "user": "root",
        "group": 0,
        "mode": "0644",
    }
    assert File("/x", mode="755").mode == "0755"
    with pytest.raises(ValidationError):
        File("/x", mode="999")
    with pytest.raises(ValidationError, match="not both"):
        File("/x", data="a", uri="https://x")
    d = Directory("/etc/app", mode="0750", ensure_parents=True)
    assert d.to_blueprint() == {"path": "/etc/app", "mode": "0750", "ensure_parents": True}


def test_repository():
    r = Repository("x", "https://repo/x", gpgcheck=True)
    assert r.to_blueprint() == {"id": "x", "baseurls": ["https://repo/x"], "gpgcheck": True}
    with pytest.raises(ValidationError, match="baseurls"):
        Repository("x")


def test_group_sshkey():
    assert Group("docker", 990).to_blueprint() == {"name": "docker", "gid": 990}
    assert SSHKey("root", "ssh-ed25519 A").merge_key == "root"


def test_services_shorthand_string():
    assert Services(enabled="sshd").enabled == ["sshd"]


def test_installer():
    i = Installer(
        unattended=True,
        sudo_nopasswd=["%wheel"],
        kickstart="text\n",
        modules={"enable": ["org.fedoraproject.Anaconda.Modules.Localization"]},
    )
    assert i.to_blueprint() == {
        "unattended": True,
        "sudo-nopasswd": ["%wheel"],
        "kickstart": {"contents": "text\n"},
        "modules": {"enable": ["org.fedoraproject.Anaconda.Modules.Localization"]},
    }
