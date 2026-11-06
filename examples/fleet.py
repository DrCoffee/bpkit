"""A realistic fleet of blueprints built from a base, mixins and overlays.

bpkit list examples/fleet.py
bpkit render examples/fleet.py -o out/
bpkit explain examples/fleet.py:StagingWeb services
"""

import os

from bpkit import (
    Blueprint,
    Container,
    Filesystem,
    Firewall,
    FirewallServices,
    Locale,
    Overlay,
    Remove,
    Replace,
    Repository,
    Services,
    Unset,
    User,
)

OPS_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOps ops@example.com"


class Base(Blueprint, abstract=True):
    """Common base for all our images."""

    distro = "rhel-9"
    packages = ["vim-enhanced", "tmux", "chrony"]
    services = Services(enabled=["sshd", "chronyd"])
    timezone = "Australia/Sydney"
    ntpservers = ["0.au.pool.ntp.org", "1.au.pool.ntp.org"]
    locale = Locale(languages=["en_AU.UTF-8"], keyboard="us")
    users = [User(name="ops", groups=["wheel"], key=OPS_KEY)]
    overlay = Overlay("overlays/base")


class Hardened(Blueprint, abstract=True):
    """Security hardening mixin."""

    packages = ["aide", "audit"]
    services = Services(enabled=["firewalld", "auditd"], masked=["cups", "avahi-daemon"])
    kernel_append = ["audit=1", "audit_backlog_limit=8192"]
    fips = True
    firewall = Firewall(services=FirewallServices(enabled=["ssh"], disabled=["dhcpv6-client"]))


class Monitored(Blueprint, abstract=True):
    """Ships a node exporter container and opens its port."""

    containers = [Container("registry.example.com/prom/node-exporter:v1.8.2")]
    firewall = Firewall(ports=["9100:tcp"])


class WebServer(Base, Hardened, Monitored):
    """Production nginx web server."""

    packages = ["nginx"]
    services = Services(enabled=["nginx"])
    firewall = Firewall(services=FirewallServices(enabled=["http", "https"]))
    timezone = "UTC"
    filesystem = [Filesystem("/var", "10 GiB"), Filesystem("/var/log", "4 GiB")]
    repositories = [
        Repository(
            "internal",
            baseurls=["https://repo.example.com/el9/$basearch/"],
            gpgkeys=["https://repo.example.com/RPM-GPG-KEY-internal"],
            gpgcheck=True,
        )
    ]
    overlay = Overlay("overlays/web")


class StagingWeb(WebServer):
    """Staging web server: debugging tools, no FIPS."""

    packages = ["htop", "strace", Remove("aide")]
    fips = Unset
    users = [User("ops", shell="/bin/zsh", groups=["wheel"], key=OPS_KEY), User("dev")]
    kernel_append = ["console=ttyS0"]

    def customize(self) -> None:
        super().customize()
        if os.environ.get("BPKIT_EXAMPLE_DEBUG"):
            self.packages.add("gdb")
            self.services.enable("debug-shell")


class MinimalWeb(WebServer):
    """Bare nginx box: drops all inherited packages."""

    packages = Replace(["nginx"])
    containers = Unset
