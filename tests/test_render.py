"""Output shape, TOML round-trip, extra_customizations and explain()."""

import tomllib

import pytest

from bpkit import (
    Blueprint,
    Container,
    Directory,
    File,
    Filesystem,
    Group,
    Installer,
    Locale,
    Remove,
    Replace,
    Repository,
    Services,
    SSHKey,
    Unset,
    User,
)


class Everything(Blueprint):
    """Uses every modelled field."""

    version = "1.0.0"
    distro = "fedora-42"
    architecture = "x86_64"
    packages = ["vim"]
    modules = ["nodejs"]
    enabled_modules = ["nodejs:20"]
    groups = ["Development Tools"]
    containers = [Container("quay.io/fedora/fedora:42", name="fedora", tls_verify=True)]
    hostname = "box"
    kernel_append = ["quiet"]
    sshkeys = [SSHKey("root", "ssh-ed25519 AAA")]
    users = [User("ops", groups=["wheel"])]
    user_groups = [Group("ops", 1000)]
    timezone = "UTC"
    ntpservers = ["time.example.com"]
    locale = Locale(["en_US.UTF-8"], "us")
    services = Services(enabled=["sshd"], default_target="multi-user.target")
    filesystem = [Filesystem("/var", "10 GiB")]
    installation_device = "/dev/vda"
    partitioning_mode = "lvm"
    directories = [Directory("/etc/app", ensure_parents=True)]
    files = [File("/etc/app/config.toml", 'key = "value"\n', mode="0640")]
    repositories = [Repository("extra", ["https://example.com/repo"])]
    fips = False
    installer = Installer(unattended=True)
    extra_customizations = {"openscap": {"profile_id": "cis"}, "kernel": {"version": "6.12"}}


EXPECTED = {
    "name": "everything",
    "description": "Uses every modelled field.",
    "version": "1.0.0",
    "distro": "fedora-42",
    "architecture": "x86_64",
    "packages": [{"name": "vim", "version": "*"}],
    "modules": [{"name": "nodejs", "version": "*"}],
    "enabled_modules": [{"name": "nodejs", "stream": "20"}],
    "groups": [{"name": "Development Tools"}],
    "containers": [{"source": "quay.io/fedora/fedora:42", "name": "fedora", "tls-verify": True}],
    "customizations": {
        "hostname": "box",
        "kernel": {"append": "quiet", "version": "6.12"},
        "sshkey": [{"user": "root", "key": "ssh-ed25519 AAA"}],
        "user": [{"name": "ops", "groups": ["wheel"]}],
        "group": [{"name": "ops", "gid": 1000}],
        "timezone": {"timezone": "UTC", "ntpservers": ["time.example.com"]},
        "locale": {"languages": ["en_US.UTF-8"], "keyboard": "us"},
        "services": {"enabled": ["sshd"], "default_target": "multi-user.target"},
        "filesystem": [{"mountpoint": "/var", "minsize": "10 GiB"}],
        "installation_device": "/dev/vda",
        "partitioning_mode": "lvm",
        "directories": [{"path": "/etc/app", "ensure_parents": True}],
        "files": [{"path": "/etc/app/config.toml", "data": 'key = "value"\n', "mode": "0640"}],
        "repositories": [{"id": "extra", "baseurls": ["https://example.com/repo"]}],
        "fips": False,
        "installer": {"unattended": True},
        "openscap": {"profile_id": "cis"},
    },
}


def test_to_dict_shape():
    assert Everything().to_dict() == EXPECTED


def test_toml_round_trip():
    bp = Everything()
    assert tomllib.loads(bp.to_toml()) == bp.to_dict() == EXPECTED


def test_output_is_deterministic():
    assert Everything().to_toml() == Everything().to_toml()
    assert list(Everything().to_dict()["customizations"])[:3] == ["hostname", "kernel", "sshkey"]


def test_empty_blueprint_omits_everything():
    class Empty(Blueprint):
        pass

    assert Empty().to_dict() == {"name": "empty", "version": "0.0.1"}
    assert tomllib.loads(Empty().to_toml()) == {"name": "empty", "version": "0.0.1"}


def test_no_markers_survive():
    class A(Blueprint):
        packages = ["a", "b"]
        services = Services(enabled=["x"], masked=["y"])

    class B(A):
        packages = [Remove("a")]
        services = Services(masked=Replace(["z"]), default_target=Unset)

    def walk(v):
        assert not isinstance(v, (Remove, Replace, type(Unset)))
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(B().to_dict())


def test_extra_customizations_deep_merge():
    class A(Blueprint):
        extra_customizations = {"openscap": {"profile_id": "cis", "datastream": "/ds.xml"}}

    class B(A):
        extra_customizations = {"openscap": {"profile_id": "stig"}, "rhsm": {"config": {}}}

    class C(B):
        extra_customizations = {"openscap": {"datastream": Unset}}

    assert B().to_dict()["customizations"] == {
        "openscap": {"profile_id": "stig", "datastream": "/ds.xml"},
    }
    assert C().to_dict()["customizations"] == {"openscap": {"profile_id": "stig"}}


def test_write(tmp_path):
    path = Everything().write(tmp_path / "out.toml")
    assert tomllib.loads(path.read_text()) == EXPECTED


def test_explain():
    class Base(Blueprint):
        packages = ["vim", "tmux"]
        services = Services(enabled=["sshd"])

    class Web(Base):
        packages = ["nginx", Remove("tmux")]

        def customize(self):
            super().customize()
            self.packages.add("curl")

    text = Web(hostname="h").explain("packages")
    assert text.splitlines() == [
        "packages:",
        "  vim    <- Base",
        "  nginx  <- Web",
        "  curl   <- customize()",
    ]
    assert "hostname:\n  'h'  <- <kwargs>" in Web(hostname="h").explain()
    assert "curl" not in Web.explain("packages")  # class-level: no customize()
    assert "enabled: sshd  <- Base" in Web.explain("services")
    with pytest.raises(KeyError, match="did you mean"):
        Web.explain("pakages")
