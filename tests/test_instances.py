"""Instance kwargs, customize() hooks, mutation helpers and isolation."""

import pytest

from bpkit import Blueprint, BlueprintError, Package, Remove, Services, Unset, User


class Base(Blueprint):
    packages = ["vim", "tmux"]
    services = Services(enabled=["sshd"])
    hostname = "base"
    users = [User("ops", groups=["wheel"])]


class Web(Base):
    packages = ["nginx"]


def names(keyed) -> list[str]:
    return [e.merge_key for e in keyed]


def test_kwargs_override_with_merge_rules():
    bp = Web(hostname="web01", packages=["curl", Remove("tmux")], timezone="UTC")
    assert bp.hostname == "web01"
    assert names(bp.packages) == ["vim", "nginx", "curl"]
    assert bp.timezone == "UTC"


def test_kwargs_identity_and_unset():
    bp = Web(name="web-prod", version="2.0.0", hostname=Unset)
    assert bp.name == "web-prod"
    assert bp.version == "2.0.0"
    assert bp.hostname is None


def test_kwargs_remove_missing_raises():
    with pytest.raises(BlueprintError, match="cannot Remove"):
        Web(packages=[Remove("emacs")])


def test_unknown_kwarg():
    with pytest.raises(BlueprintError, match="did you mean 'hostname'"):
        Web(hostnme="x")


def test_customize_with_super_chaining(monkeypatch):
    calls = []

    class Monitored(Blueprint, abstract=True):
        def customize(self):
            super().customize()
            calls.append("Monitored")
            self.packages.add("node-exporter")
            self.services.enable("node-exporter")

    class App(Monitored, Web):
        def customize(self):
            super().customize()
            calls.append("App")
            self.packages.remove("tmux")
            self.services.mask("sshd")
            self.hostname = f"app-{self.version}"

    bp = App()
    assert calls == ["Monitored", "App"]
    assert names(bp.packages) == ["vim", "nginx", "node-exporter"]
    assert bp.services.enabled == ["node-exporter"]
    assert bp.services.masked == ["sshd"]
    assert bp.hostname == "app-0.0.1"


def test_customize_reads_environment(monkeypatch):
    class Env(Web):
        def customize(self):
            super().customize()
            import os

            if os.environ.get("DEBUG_IMAGE"):
                self.packages.add("gdb", Package("strace", "6*"))

    assert "gdb" not in Env().packages
    monkeypatch.setenv("DEBUG_IMAGE", "1")
    bp = Env()
    assert "gdb" in bp.packages
    assert bp.packages["strace"].version == "6*"


def test_helpers():
    bp = Web()
    bp.packages.add("vim")  # no duplicate
    assert len(bp.packages) == 3
    bp.packages.add(Package("vim", "9.1*"))  # replaces in place
    assert bp.packages[0].version == "9.1*"
    with pytest.raises(KeyError):
        bp.packages.remove("emacs")
    bp.packages.discard("emacs")
    bp.kernel_append.add("quiet")
    bp.ntpservers.add("pool.ntp.org")
    bp.firewall.open_port("443:tcp")
    bp.firewall.services.enable("https")
    bp.locale.languages.append("en_GB.UTF-8")
    c = bp.to_dict()["customizations"]
    assert c["kernel"]["append"] == "quiet"
    assert c["timezone"] == {"ntpservers": ["pool.ntp.org"]}
    assert c["firewall"] == {"ports": ["443:tcp"], "services": {"enabled": ["https"]}}
    assert c["locale"] == {"languages": ["en_GB.UTF-8"]}


def test_assignment_replaces_and_validates():
    bp = Web()
    bp.packages = ["htop"]
    assert names(bp.packages) == ["htop"]
    bp.timezone = "UTC"
    bp.hostname = Unset
    assert bp.hostname is None
    with pytest.raises(BlueprintError, match=r"use self\.packages\.remove"):
        bp.packages = [Remove("htop")]
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        bp.fips = "nope"


def test_instance_mutation_does_not_leak():
    a = Web()
    a.packages.add("emacs")
    a.services.enable("httpd")
    a.users["ops"].groups.append("docker")
    a.hostname = "changed"

    b = Web()
    sibling = Base()
    for bp in (b, sibling):
        assert "emacs" not in bp.packages
        assert bp.services.enabled == ["sshd"]
        assert bp.users["ops"].groups == ["wheel"]
        assert bp.hostname == "base"
    assert Web._resolved["packages"].keys() == ["vim", "tmux", "nginx"]
    # the class's own declaration is untouched too
    assert Base.users[0].groups == ["wheel"]


def test_class_declaration_objects_are_not_shared():
    shared = Services(enabled=["a"])

    class A(Blueprint):
        services = shared

    A().services.enable("b")
    assert shared.enabled == ["a"]


def test_equality_and_repr():
    assert Web() == Web()
    assert Web() != Web(hostname="other")
    assert repr(Web()) == "<Web 'web' v0.0.1>"
