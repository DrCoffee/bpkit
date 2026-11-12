"""Audit blueprints: config mistakes, risky settings, overlay usage, advisories.

``bpkit audit fleet.py`` runs the offline checks on every concrete class.
Each finding has a stable code (``BP0xx``), a severity, the field it is about
and, where known, the class that set the value::

    error    BP040 files /usr/lib/x.conf: paths under /usr are not allowed by image-builder
    warning  BP011 users ops: password is plain text; use a crypt hash  (WebServer)

Silence a code for a class and its subclasses with ``AUDIT_IGNORE = ["BP046"]``
in the class body, or for a whole run with ``--ignore BP046``.

Advisories (``--advisories``) need exact package versions. bpkit gets them by
depsolving through osbuild-composer (``--depsolve composer``) or from an
``rpm -qa`` listing (``--packages FILE``), then queries OSV.dev. OSV covers
RHEL, AlmaLinux and Rocky Linux; Fedora and CentOS Stream aren't covered.

| Code | Severity | Check |
|---|---|---|
| BP001 | error | invalid hostname |
| BP002 | error | unknown timezone |
| BP003 | warning | locale language doesn't look like `xx_YY.UTF-8` |
| BP010 | warning | user has no SSH key or password |
| BP011 | warning | plain-text password |
| BP012 | info | user group not created by the blueprint |
| BP013 | warning | root has a password |
| BP020 | error | invalid firewall port |
| BP021 | warning | firewall rules set but firewalld disabled/masked |
| BP022 | warning | SSH keys set but sshd disabled/masked |
| BP030 | error | mountpoint image-builder won't accept |
| BP031 | warning | filesystem under 1 MiB (missing units?) |
| BP040 | error | file/directory path image-builder won't accept |
| BP041 | error | file under another file, or file and directory at one path |
| BP042 | warning | world-writable or setuid/setgid mode |
| BP043 | warning | sudoers drop-in not mode 0440 |
| BP044 | warning | file contents look like a secret |
| BP045 | warning | inline file over 512 KiB |
| BP046 | info | file's parent directory isn't created by the blueprint |
| BP050 | warning | repository over plain HTTP |
| BP051 | warning | repository GPG checking off or without keys |
| BP052 | warning | repository TLS verification off |
| BP053 | warning | container TLS verification off |
| BP060 | warning | kernel argument that weakens security |
| BP061 | warning | FIPS on a distro that doesn't support it |
| BP070 | warning | risky package (telnet-server, rsh-server, …) |
| BP071 | info | no distro set |
| BP080 | warning | not expressible on the hosted service (`--hosted`) |
| BP090 | info | explicit File hides an overlay's version |
| BP091 | warning | overlay contributes nothing |
| BP092 | info | overlay file replaced by a later overlay |
| BP100 | warning | known advisory affects a resolved package |
| BP101 | info | no exact versions to check advisories against |
| BP102 | info | no advisory data for this distro |
| BP103 | warning | OSV.dev unreachable |
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .merge import STACKS
from .overlay import check_path_policy

if TYPE_CHECKING:
    from .blueprint import Blueprint

__all__ = [
    "AuditReport",
    "Finding",
    "ResolvedPackage",
    "audit",
    "check_advisories",
    "load_package_list",
    "osv_ecosystems",
    "resolve_with_composer",
]

Severity = Literal["error", "warning", "info"]
_RANK = {"error": 0, "warning": 1, "info": 2}


@dataclass(frozen=True)
class Finding:
    severity: Severity
    code: str
    field: str
    message: str
    origin: str | None = None
    hint: str | None = None


@dataclass
class ResolvedPackage:
    """An exact package version (from a depsolve or ``rpm -qa``)."""

    name: str
    epoch: int
    version: str
    release: str
    arch: str | None = None
    source: str | None = None  # source package name, if known
    sha256: str | None = None  # RPM file digest, if known (image-builder manifests)

    @property
    def evr(self) -> str:
        return f"{self.epoch}:{self.version}-{self.release}"

    def __str__(self) -> str:
        arch = f".{self.arch}" if self.arch else ""
        return f"{self.name}-{self.evr}{arch}"


@dataclass
class AuditReport:
    blueprint: str
    findings: list[Finding] = field(default_factory=list)
    packages: list[ResolvedPackage] = field(default_factory=list)
    #: Codes dropped on ``add`` (``--ignore`` plus the classes' ``AUDIT_IGNORE``).
    ignore: set[str] = field(default_factory=set)

    def add(self, *findings: Finding) -> None:
        self.findings.extend(f for f in findings if f.code not in self.ignore)

    def sorted(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: (_RANK[f.severity], f.code, f.field, f.message))

    def count(self, severity: Severity) -> int:
        return sum(1 for f in self.findings if f.severity == severity)

    def failed(self, *, strict: bool = False) -> bool:
        return self.count("error") > 0 or (strict and self.count("warning") > 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "blueprint": self.blueprint,
            "summary": {s: self.count(s) for s in ("error", "warning", "info")},
            "findings": [
                {k: v for k, v in asdict(f).items() if v is not None} for f in self.sorted()
            ],
            "packages": [str(p) for p in self.packages],
        }

    def to_text(self, *, show_info: bool = True) -> str:
        lines = [f"{self.blueprint}:"]
        shown = [f for f in self.sorted() if show_info or f.severity != "info"]
        if not shown:
            lines.append("  no findings")
        for f in shown:
            origin = f"  ({f.origin})" if f.origin else ""
            lines.append(f"  {f.severity:<8} {f.code} {f.field}: {f.message}{origin}")
            if f.hint:
                lines.append(f"           hint: {f.hint}")
        s = {k: self.count(k) for k in ("error", "warning", "info")}
        lines.append(f"  -- {s['error']} error(s), {s['warning']} warning(s), {s['info']} info")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Context helpers
# --------------------------------------------------------------------------


class _Ctx:
    def __init__(self, bp: Blueprint) -> None:
        self.bp = bp
        self.prov: dict[str, dict[str, str]] = bp.__dict__.get("_provenance", {})
        self.stacks: dict[str, Any] = bp.__dict__.get(STACKS, {})

    def origin(self, fname: str, key: Any = None) -> str | None:
        atoms = self.prov.get(fname, {})
        if key is None:
            return atoms.get("") or next(iter(atoms.values()), None)
        label = atoms.get(f"[{key!r}]") or atoms.get(f"[{key}]")
        stack = (
            (self.stacks.get(fname) or {}).get(key) if fname in ("files", "directories") else None
        )
        if stack and stack[-1][0]:
            root = stack[-1][0]
            short = os.path.relpath(root) if not os.path.relpath(root).startswith("..") else root
            return f"{label}, overlay {short}" if label else f"overlay {short}"
        return label

    def sub_origin(self, fname: str, sub: str) -> str | None:
        atoms = self.prov.get(fname, {})
        for atom_id, label in atoms.items():
            if atom_id.startswith(sub):
                return label
        return None


# --------------------------------------------------------------------------
# Offline checks
# --------------------------------------------------------------------------

_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$"
)
_LANG = re.compile(r"^[a-z]{2,3}(_[A-Z]{2})?(\.[A-Za-z0-9-]+)?(@\w+)?$|^C(\.UTF-8)?$|^POSIX$")
_PORT = re.compile(r"^(?:\d{1,5}(?:-\d{1,5})?|[a-z][a-z0-9-]*):(?:tcp|udp|sctp|dccp|any)$")
_SYSTEM_GROUPS = frozenset(
    [
        "wheel",
        "adm",
        "users",
        "systemd-journal",
        "audio",
        "video",
        "dialout",
        "docker",
        "libvirt",
        "kvm",
        "input",
        "render",
        "disk",
        "lp",
        "mail",
        "floppy",
        "cdrom",
        "tape",
        "games",
        "ftp",
        "nobody",
    ]
)
_WEAK_KERNEL_ARGS = {
    "selinux=0": "disables SELinux",
    "enforcing=0": "puts SELinux in permissive mode",
    "audit=0": "disables the audit subsystem",
    "mitigations=off": "disables CPU vulnerability mitigations",
    "nosmep": "disables SMEP",
    "nosmap": "disables SMAP",
    "nokaslr": "disables KASLR",
    "init=/bin/sh": "boots straight to a shell",
    "init=/bin/bash": "boots straight to a shell",
}
_RISKY_PACKAGES = {
    "telnet-server": "unencrypted remote login",
    "rsh-server": "unencrypted remote login",
    "rsh": "unencrypted remote shell client",
    "tftp-server": "unauthenticated file transfer",
    "ypserv": "NIS server (weak authentication)",
    "ypbind": "NIS client (weak authentication)",
    "xinetd": "legacy super-server",
    "talk-server": "legacy, unencrypted",
}
_SECRET_PATTERNS = [
    (
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"),
        "a private key",
    ),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "an AWS access key id"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), "a GitHub token"),
    (
        re.compile(r"(?im)^\s*(?:password|passwd|secret|api[_-]?key|token)\s*[=:]\s*\S{6,}"),
        "a password or token",
    ),
]
# Directories image-builder (or every distro) provides, for BP046.
_STANDARD_DIRS = (
    "/etc",
    "/etc/profile.d",
    "/etc/sudoers.d",
    "/etc/ssh",
    "/etc/ssh/sshd_config.d",
    "/etc/systemd/system",
    "/etc/systemd",
    "/etc/sysctl.d",
    "/etc/modprobe.d",
    "/etc/security",
    "/etc/pki",
    "/etc/pki/tls",
    "/etc/ssl",
    "/etc/pki/ca-trust/source/anchors",
    "/etc/yum.repos.d",
    "/etc/sysconfig",
    "/etc/tmpfiles.d",
    "/etc/udev/rules.d",
    "/etc/motd.d",
    "/etc/cron.d",
    "/etc/logrotate.d",
    "/etc/dnf",
    "/etc/NetworkManager/system-connections",
    "/etc/NetworkManager/conf.d",
    "/etc/containers",
    "/etc/environment.d",
    "/etc/skel",
    "/etc/default",
    "/usr/local/bin",
    "/usr/local/sbin",
    "/usr/local/lib",
    "/usr/local/share",
    "/usr/local/etc",
    "/root",
    "/root/.ssh",
    "/opt",
    "/srv",
    "/var",
    "/var/lib",
    "/var/log",
    "/var/www",
)
# image-builder's MountpointPolicies (osbuild/images pkg/policies).
_MOUNT_DENY = (
    "/etc",
    "/sys",
    "/proc",
    "/dev",
    "/run",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/lost+found",
    "/sysroot",
    "/var/run",
    "/var/lock",
)


def _mountpoint_problem(mp: str) -> str | None:
    if mp.startswith("/usr/") or mp == "/usr/":
        return "mountpoints under /usr aren't supported by the initrd fstab generator"
    for p in _MOUNT_DENY:
        if mp == p or mp.startswith(p + "/"):
            return f"{p} can't be a separate mountpoint"
    return None


def _mode_bits(mode: str | None) -> int | None:
    return int(mode, 8) if mode else None


def _check_identity(c: _Ctx, r: AuditReport) -> None:
    bp = c.bp
    if bp.hostname is not None and not _HOSTNAME.match(bp.hostname):
        r.add(
            Finding(
                "error",
                "BP001",
                "hostname",
                f"{bp.hostname!r} is not a valid hostname",
                c.origin("hostname"),
                "letters, digits and hyphens; labels of 1-63 chars",
            )
        )
    if bp.timezone is not None:
        try:
            import zoneinfo

            zones = zoneinfo.available_timezones()
        except Exception:  # pragma: no cover - no tz database
            zones = set()
        if zones and bp.timezone not in zones and bp.timezone != "UTC":
            r.add(
                Finding(
                    "error",
                    "BP002",
                    "timezone",
                    f"unknown timezone {bp.timezone!r}",
                    c.origin("timezone"),
                    "use an IANA name like 'Europe/Berlin'",
                )
            )
    for lang in bp.locale.languages or []:
        if not _LANG.match(lang):
            r.add(
                Finding(
                    "warning",
                    "BP003",
                    "locale",
                    f"{lang!r} doesn't look like a locale",
                    c.sub_origin("locale", "languages"),
                    "e.g. 'en_US.UTF-8'",
                )
            )
    if not bp.distro:
        r.add(
            Finding(
                "info", "BP071", "distro", "no distro set; image-builder will use the host's distro"
            )
        )
    if bp.fips is True and (bp.distro or "").startswith("fedora"):
        r.add(
            Finding(
                "warning", "BP061", "fips", "FIPS mode isn't supported on Fedora", c.origin("fips")
            )
        )


def _check_users(c: _Ctx, r: AuditReport) -> None:
    bp = c.bp
    keyed = {k.user for k in bp.sshkeys}
    created_groups = {g.name for g in bp.user_groups}
    for u in bp.users:
        o = c.origin("users", u.name)
        if not u.key and not u.password and u.name not in keyed:
            r.add(
                Finding(
                    "warning",
                    "BP010",
                    f"users {u.name}",
                    "no SSH key or password; this user can't log in",
                    o,
                )
            )
        if u.password and not u.password.startswith("$"):
            r.add(
                Finding(
                    "warning",
                    "BP011",
                    f"users {u.name}",
                    "password is plain text in the blueprint",
                    o,
                    "use a crypt hash, e.g. `openssl passwd -6`",
                )
            )
        if u.name == "root" and u.password:
            r.add(
                Finding(
                    "warning",
                    "BP013",
                    "users root",
                    "root has a password set",
                    o,
                    "prefer an SSH key and a wheel user",
                )
            )
        for g in u.groups or []:
            if g not in created_groups and g not in _SYSTEM_GROUPS:
                r.add(
                    Finding(
                        "info",
                        "BP012",
                        f"users {u.name}",
                        f"group {g!r} isn't created by this blueprint",
                        o,
                        "add it to user_groups unless a package creates it",
                    )
                )


def _check_network(c: _Ctx, r: AuditReport) -> None:
    bp = c.bp
    fw = bp.firewall
    for port in fw.ports or []:
        if not _PORT.match(port):
            r.add(
                Finding(
                    "error",
                    "BP020",
                    "firewall",
                    f"invalid port {port!r}",
                    c.sub_origin("firewall", f"ports[{port}]"),
                    "use 'PORT:PROTO', 'START-END:PROTO' or 'SERVICE:PROTO'",
                )
            )
        else:
            num = port.split(":")[0].split("-")
            if all(n.isdigit() for n in num) and any(int(n) > 65535 for n in num):
                r.add(Finding("error", "BP020", "firewall", f"port out of range in {port!r}"))
    fw_used = bool(fw.ports or fw.services.enabled or fw.services.disabled or fw.zones)
    svc = bp.services
    off = set(svc.disabled or []) | set(svc.masked or [])
    if fw_used and "firewalld" in off:
        r.add(
            Finding(
                "warning",
                "BP021",
                "services",
                "firewall rules are set but firewalld is disabled or masked",
                c.sub_origin("services", "masked[firewalld]")
                or c.sub_origin("services", "disabled[firewalld]"),
            )
        )
    has_keys = any(u.key for u in bp.users) or bool(bp.sshkeys)
    if has_keys and "sshd" in off:
        r.add(
            Finding(
                "warning",
                "BP022",
                "services",
                "SSH keys are configured but sshd is disabled or masked",
                c.sub_origin("services", "masked[sshd]")
                or c.sub_origin("services", "disabled[sshd]"),
            )
        )


def _check_storage(c: _Ctx, r: AuditReport) -> None:
    for fs in c.bp.filesystem:
        o = c.origin("filesystem", fs.mountpoint)
        if why := _mountpoint_problem(fs.mountpoint):
            r.add(Finding("error", "BP030", f"filesystem {fs.mountpoint}", why, o))
        if fs.size_bytes < 1024**2:
            r.add(
                Finding(
                    "warning",
                    "BP031",
                    f"filesystem {fs.mountpoint}",
                    f"minsize is only {fs.size_bytes} bytes",
                    o,
                    "sizes without units are bytes; did you mean '10 GiB'?",
                )
            )


def _check_fs_nodes(c: _Ctx, r: AuditReport) -> None:
    bp = c.bp
    dirs = {d.path: d for d in bp.directories}
    files = {f.path: f for f in bp.files}
    for kind, nodes in (("directories", dirs), ("files", files)):
        for path, node in nodes.items():
            o = c.origin(kind, path)
            label = f"{kind} {path}"
            if why := check_path_policy(path, is_file=kind == "files"):
                r.add(Finding("error", "BP040", label, why, o))
            bits = _mode_bits(node.mode)
            if bits is not None:
                if bits & 0o002 and not (kind == "directories" and bits & 0o1000):
                    r.add(Finding("warning", "BP042", label, f"world-writable mode {node.mode}", o))
                if bits & 0o6000 and kind == "files":
                    r.add(Finding("warning", "BP042", label, f"setuid/setgid mode {node.mode}", o))
            if kind == "files" and path.startswith("/etc/sudoers.d/") and (bits or 0o644) & 0o137:
                r.add(
                    Finding(
                        "warning",
                        "BP043",
                        label,
                        f"sudoers drop-ins should be mode 0440 (is {node.mode or '0644'})",
                        o,
                        "sudo ignores or rejects files with looser modes",
                    )
                )
    for path in files:
        parent = path
        while (parent := os.path.dirname(parent)) not in ("/", ""):
            if parent in files:
                r.add(
                    Finding(
                        "error",
                        "BP041",
                        f"files {path}",
                        f"parent {parent} is a file, not a directory",
                        c.origin("files", path),
                    )
                )
                break
        if path in dirs:
            r.add(
                Finding(
                    "error",
                    "BP041",
                    f"files {path}",
                    "the same path is also a directory entry",
                    c.origin("files", path),
                )
            )
    for path, f in files.items():
        o = c.origin("files", path)
        data = f.data or ""
        if len(data.encode()) > 512 * 1024:
            r.add(
                Finding(
                    "warning",
                    "BP045",
                    f"files {path}",
                    f"{len(data.encode()) // 1024} KiB inline; blueprints get slow and "
                    "hard to diff",
                    o,
                    "ship it in a package or fetch it at first boot",
                )
            )
        for pattern, what in _SECRET_PATTERNS:
            if pattern.search(data):
                r.add(
                    Finding(
                        "warning",
                        "BP044",
                        f"files {path}",
                        f"contents look like {what}; the blueprint (and image) will "
                        "contain it in clear text",
                        o,
                        "inject secrets at deploy time (cloud-init, a vault agent)",
                    )
                )
                break
        parent = os.path.dirname(path)
        # A Directory entry at or below the parent creates it (ensure_parents).
        covered = parent in _STANDARD_DIRS or any(
            d == parent or d.startswith(parent + "/") for d in dirs
        )
        if not covered and parent != "/":
            r.add(
                Finding(
                    "info",
                    "BP046",
                    f"files {path}",
                    f"parent {parent} isn't created by this blueprint",
                    o,
                    f"add Directory({parent!r}, ensure_parents=True) unless a package creates it",
                )
            )


def _check_sources(c: _Ctx, r: AuditReport) -> None:
    for repo in c.bp.repositories:
        o = c.origin("repositories", repo.id)
        label = f"repositories {repo.id}"
        for url in [*(repo.baseurls or []), repo.metalink, repo.mirrorlist]:
            if url and url.startswith("http://"):
                r.add(
                    Finding(
                        "warning",
                        "BP050",
                        label,
                        f"plain HTTP URL {url}",
                        o,
                        "use https:// so metadata can't be tampered with",
                    )
                )
        if repo.gpgcheck is False:
            r.add(Finding("warning", "BP051", label, "gpgcheck is disabled", o))
        elif repo.gpgcheck and not repo.gpgkeys:
            r.add(Finding("warning", "BP051", label, "gpgcheck is on but no gpgkeys are set", o))
        if repo.sslverify is False:
            r.add(Finding("warning", "BP052", label, "sslverify is disabled", o))
    for ctr in c.bp.containers:
        if ctr.tls_verify is False:
            r.add(
                Finding(
                    "warning",
                    "BP053",
                    f"containers {ctr.source}",
                    "tls-verify is disabled",
                    c.origin("containers", ctr.source),
                )
            )


def _check_kernel_and_packages(c: _Ctx, r: AuditReport) -> None:
    for arg in c.bp.kernel_append:
        if arg in _WEAK_KERNEL_ARGS:
            r.add(
                Finding(
                    "warning",
                    "BP060",
                    "kernel_append",
                    f"{arg} {_WEAK_KERNEL_ARGS[arg]}",
                    c.prov.get("kernel_append", {}).get(f"[{arg}]"),
                )
            )
    for p in c.bp.packages:
        if p.name in _RISKY_PACKAGES:
            r.add(
                Finding(
                    "warning",
                    "BP070",
                    f"packages {p.name}",
                    f"installs {p.name}: {_RISKY_PACKAGES[p.name]}",
                    c.origin("packages", p.name),
                )
            )


def _check_overlays(c: _Ctx, r: AuditReport) -> None:
    stacks = c.stacks
    if not stacks:
        return
    owned_roots: set[str] = set()
    for kind in ("files", "directories"):
        for path, entries in (stacks.get(kind) or {}).items():
            owned_roots.update(e[0] for e in entries if e[0])
            if kind != "files" or len(entries) < 2:
                continue
            top_owner, _, top_label = entries[-1]
            hidden = [e for e in entries[:-1] if e[0]]
            if top_owner is None and hidden:
                r.add(
                    Finding(
                        "info",
                        "BP090",
                        f"files {path}",
                        f"an explicit File in {top_label} hides the overlay version from "
                        f"{hidden[-1][2]}",
                        top_label,
                    )
                )
            elif top_owner and hidden and hidden[-1][0] != top_owner:
                r.add(
                    Finding(
                        "info",
                        "BP092",
                        f"files {path}",
                        f"overlay in {top_label} replaces the one from {hidden[-1][2]}",
                        top_label,
                    )
                )
    for root in stacks.get("roots", []):
        if root not in owned_roots:
            r.add(
                Finding(
                    "warning",
                    "BP091",
                    "overlay",
                    f"overlay {os.path.relpath(root)} contributes no files or directories",
                    hint="check its exclude patterns, or drop it",
                )
            )


def _check_hosted(c: _Ctx, r: AuditReport) -> None:
    from .hosted import HostedUnsupportedError, to_hosted_customizations

    try:
        to_hosted_customizations(c.bp.to_dict())
    except HostedUnsupportedError as e:
        for problem in e.problems:
            r.add(
                Finding(
                    "warning", "BP080", "hosted", f"not supported by the hosted service: {problem}"
                )
            )


CHECKS = (
    _check_identity,
    _check_users,
    _check_network,
    _check_storage,
    _check_fs_nodes,
    _check_sources,
    _check_kernel_and_packages,
    _check_overlays,
)


def _ignored(bp: Blueprint) -> set[str]:
    codes: set[str] = set()
    for klass in type(bp).__mro__:
        codes.update(klass.__dict__.get("AUDIT_IGNORE", ()))
    return codes


def audit(bp: Blueprint, *, hosted: bool = False, ignore: Iterable[str] = ()) -> AuditReport:
    """Run the offline checks on a blueprint instance."""
    ctx = _Ctx(bp)
    # The report keeps the ignore set, so check_advisories() honours it too.
    report = AuditReport(bp.name, ignore=set(ignore) | _ignored(bp))
    for check in CHECKS:
        check(ctx, report)
    if hosted:
        _check_hosted(ctx, report)
    return report


# --------------------------------------------------------------------------
# Package versions
# --------------------------------------------------------------------------

_NEVRA = re.compile(
    r"^(?P<name>.+)-(?:(?P<epoch>\d+):)?(?P<version>[^-:]+)-(?P<release>[^-]+?)"
    r"(?:\.(?P<arch>x86_64|aarch64|ppc64le|s390x|noarch|i686|src))?(?:\.rpm)?$"
)


def _source_name(srpm: str | None) -> str | None:
    if not srpm or srpm == "(none)":
        return None
    m = _NEVRA.match(srpm.removesuffix(".src.rpm").removesuffix(".rpm"))
    return m.group("name") if m else None


def load_package_list(path: str | Path) -> list[ResolvedPackage]:
    """Read installed-package versions.

    Accepts ``rpm -qa --qf '%{NAME} %{EPOCHNUM}:%{VERSION}-%{RELEASE} %{SOURCERPM}\\n'``
    output (recommended; includes source packages) or plain NEVRA lines
    (``openssl-libs-1:3.0.7-27.el9.x86_64``).
    """
    out: list[ResolvedPackage] = []
    for lineno, raw in enumerate(Path(path).read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2 and ":" in parts[1] and "-" in parts[1]:
            epoch, _, vr = parts[1].partition(":")
            version, _, release = vr.rpartition("-")
            out.append(
                ResolvedPackage(
                    parts[0],
                    int(epoch or 0),
                    version,
                    release,
                    source=_source_name(parts[2] if len(parts) > 2 else None),
                )
            )
            continue
        m = _NEVRA.match(parts[0])
        if not m:
            raise ValueError(f"{path}:{lineno}: can't parse {line!r}")
        out.append(
            ResolvedPackage(m["name"], int(m["epoch"] or 0), m["version"], m["release"], m["arch"])
        )
    return out


def resolve_with_composer(bp: Blueprint, client: Any) -> list[ResolvedPackage]:
    """Push the blueprint to osbuild-composer and depsolve it.

    The push creates the blueprint in osbuild-composer's store, replacing one
    with the same name.
    """
    name = client.push(bp)
    reply = client.depsolve(name)
    entries = reply.get("blueprints", [])
    # A failed depsolve still returns the entry, with no dependencies.
    if reply.get("errors") or not entries:
        raise RuntimeError(f"depsolve failed for {name}: {reply.get('errors')}")
    return [
        ResolvedPackage(
            d["name"], int(d.get("epoch") or 0), d["version"], d["release"], d.get("arch")
        )
        for d in entries[0].get("dependencies", [])
    ]


def pinned_packages(bp: Blueprint) -> list[ResolvedPackage]:
    """Packages pinned to an exact ``[epoch:]version-release`` in the blueprint."""
    out = []
    for p in bp.packages:
        v = p.version
        if not v or any(ch in v for ch in "*?[") or "-" not in v:
            continue
        epoch, _, vr = v.rpartition(":") if ":" in v else ("0", "", v)
        version, _, release = vr.rpartition("-")
        out.append(ResolvedPackage(p.name, int(epoch or 0), version, release))
    return out


# --------------------------------------------------------------------------
# Advisories (OSV.dev)
# --------------------------------------------------------------------------

OSV_URL = "https://api.osv.dev"
_SOURCE_SUFFIXES = (
    "-libs",
    "-devel",
    "-common",
    "-core",
    "-tools",
    "-utils",
    "-server",
    "-client",
    "-headers",
    "-data",
    "-doc",
    "-fips-provider",
)


def osv_ecosystems(distro: str | None) -> list[str] | None:
    """OSV ecosystems for a blueprint distro, or ``None`` if OSV doesn't cover it."""
    if not distro:
        return None
    m = re.match(r"^(rhel|almalinux|alma|rocky)-?(\d+)", distro)
    if not m:
        return None
    family, digits = m.group(1), m.group(2)
    # rhel-9 / rhel-9.4 / rhel-94 (9.4) / rhel-810 (8.10) / rhel-10 / rhel-100 (10.0)
    major = digits if len(digits) == 1 else ("10" if digits.startswith("10") else digits[0])
    if family == "rhel":
        return [
            f"Red Hat:enterprise_linux:{major}::baseos",
            f"Red Hat:enterprise_linux:{major}::appstream",
        ]
    if family in ("alma", "almalinux"):
        return [f"AlmaLinux:{major}"]
    return [f"Rocky Linux:{major}"]


def _query_names(pkg: ResolvedPackage, ecosystem: str) -> list[str]:
    """Red Hat records use binary names; Alma/Rocky use source package names."""
    if ecosystem.startswith("Red Hat"):
        return [pkg.name]
    if pkg.source:
        return [pkg.source]
    names = [pkg.name]
    for suffix in _SOURCE_SUFFIXES:
        if pkg.name.endswith(suffix):
            names.append(pkg.name.removesuffix(suffix))
    return names


def _post(url: str, payload: Any, timeout: float) -> Any:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _get(url: str, timeout: float) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def _fixed_in(vuln: Mapping[str, Any], name: str, ecosystems: Sequence[str]) -> str | None:
    for affected in vuln.get("affected", []):
        pkg = affected.get("package", {})
        if pkg.get("name") != name or pkg.get("ecosystem") not in ecosystems:
            continue
        for rng in affected.get("ranges", []):
            for event in rng.get("events", []):
                if "fixed" in event:
                    return str(event["fixed"])
    return None


def check_advisories(
    report: AuditReport,
    packages: Sequence[ResolvedPackage],
    distro: str | None,
    *,
    osv_url: str | None = None,
    timeout: float = 30,
    max_details: int = 200,
) -> None:
    """Query OSV.dev for known advisories affecting ``packages``; add BP100 findings."""
    report.packages = list(packages)
    ecosystems = osv_ecosystems(distro)
    if ecosystems is None:
        report.add(
            Finding(
                "info",
                "BP102",
                "advisories",
                f"OSV.dev has no advisory data for distro {distro!r}",
                hint="advisories are available for rhel-*, almalinux-* and rocky-*",
            )
        )
        return
    if not packages:
        report.add(
            Finding(
                "info",
                "BP101",
                "advisories",
                "no exact package versions to check",
                hint="use --depsolve composer or --packages FILE (rpm -qa output)",
            )
        )
        return
    base = (osv_url or os.environ.get("BPKIT_OSV_URL") or OSV_URL).rstrip("/")
    queries: list[dict[str, Any]] = []
    owners: list[tuple[ResolvedPackage, str, str]] = []
    for pkg in packages:
        for eco in ecosystems:
            for qname in _query_names(pkg, eco):
                queries.append({"package": {"name": qname, "ecosystem": eco}, "version": pkg.evr})
                owners.append((pkg, qname, eco))

    hits: dict[str, list[tuple[ResolvedPackage, str]]] = {}
    try:
        for start in range(0, len(queries), 1000):
            reply = _post(
                f"{base}/v1/querybatch", {"queries": queries[start : start + 1000]}, timeout
            )
            for (pkg, qname, _), result in zip(
                owners[start : start + 1000], reply.get("results", []), strict=False
            ):
                for v in result.get("vulns", []) or []:
                    hits.setdefault(v["id"], [])
                    if (pkg, qname) not in hits[v["id"]]:
                        hits[v["id"]].append((pkg, qname))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        report.add(
            Finding(
                "warning",
                "BP103",
                "advisories",
                f"couldn't query OSV.dev: {e}",
                hint="set BPKIT_OSV_URL for a mirror, or check network access",
            )
        )
        return

    for i, (vid, affected) in enumerate(sorted(hits.items())):
        summary, fixed = "", None
        if i < max_details:
            try:
                vuln = _get(f"{base}/v1/vulns/{vid}", timeout)
                summary = vuln.get("summary") or (vuln.get("details") or "")[:120]
                fixed = _fixed_in(vuln, affected[0][1], ecosystems)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                pass
        pkgs = ", ".join(sorted({str(p) for p, _ in affected}))
        msg = f"{vid} affects {pkgs}" + (f": {summary}" if summary else "")
        report.add(
            Finding(
                "warning", "BP100", "advisories", msg, hint=f"fixed in {fixed}" if fixed else None
            )
        )
