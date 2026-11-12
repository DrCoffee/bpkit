"""bpkit audit: offline checks, overlay usage, package advisories."""

import json

import pytest

from bpkit import (
    Blueprint,
    Container,
    Directory,
    File,
    Filesystem,
    Firewall,
    Locale,
    Overlay,
    Package,
    Remove,
    Repository,
    Services,
    User,
)
from bpkit.audit import (
    ResolvedPackage,
    audit,
    check_advisories,
    load_package_list,
    osv_ecosystems,
    pinned_packages,
)
from bpkit.cli import main


def codes(report, severity=None):
    return sorted({f.code for f in report.findings if severity in (None, f.severity)})


def find(report, code):
    return [f for f in report.findings if f.code == code]


class Clean(Blueprint):
    distro = "rhel-9"
    hostname = "web01.example.com"
    timezone = "Europe/Berlin"
    locale = Locale(["en_US.UTF-8"])
    packages = ["nginx"]
    users = [User("ops", groups=["wheel"], key="ssh-ed25519 AAA")]
    services = Services(enabled=["sshd", "firewalld"])
    firewall = Firewall(ports=["22:tcp", "8000-8100:tcp", "https:tcp"])
    filesystem = [Filesystem("/var", "10 GiB")]
    directories = [Directory("/etc/myapp", ensure_parents=True)]
    files = [File("/etc/myapp/app.conf", "level = info\n", mode="0640")]
    repositories = [Repository("r", ["https://r/x"], gpgcheck=True, gpgkeys=["https://r/k"])]


def test_clean_blueprint_has_no_findings():
    assert Clean().explain() and audit(Clean()).findings == []


class Messy(Blueprint):
    hostname = "-bad_host"
    timezone = "Mars/Olympus"
    locale = Locale(["english"])
    distro = "fedora-42"
    fips = True
    packages = ["telnet-server"]
    users = [
        User("nobody-login"),
        User("ops", password="hunter2", groups=["devs"]),
        User("root", password="$6$hash"),
    ]
    sshkeys = [{"user": "root", "key": "ssh-ed25519 AAA"}]
    services = Services(masked=["sshd", "firewalld"])
    firewall = Firewall(ports=["22", "70000:tcp"])
    filesystem = [Filesystem("/etc", "1 GiB"), Filesystem("/data", 10)]
    kernel_append = ["selinux=0", "mitigations=off"]
    directories = [Directory("/srv/drop", mode="0777")]
    files = [
        File("/etc/sudoers.d/ops", "ops ALL=(ALL) NOPASSWD: ALL\n"),
        File("/usr/local/bin/suid", "#!/bin/sh\n", mode="4755"),
        File("/etc/app/secret.env", "API_KEY=abcdef123456\n"),
        File("/etc/ssl/key.pem", "-----BEGIN PRIVATE KEY-----\nx\n"),
        File("/opt/myapp/conf/x.conf", "x"),
        File("/etc/big.txt", "x" * (600 * 1024)),
    ]
    repositories = [Repository("insecure", ["http://mirror/x"], gpgcheck=False, sslverify=False)]
    containers = [Container("registry.local/x", tls_verify=False)]


def test_messy_blueprint_findings():
    r = audit(Messy())
    assert codes(r, "error") == ["BP001", "BP002", "BP020", "BP030"]
    assert codes(r, "warning") == [
        "BP003",
        "BP010",
        "BP011",
        "BP013",
        "BP021",
        "BP022",
        "BP031",
        "BP042",
        "BP043",
        "BP044",
        "BP045",
        "BP050",
        "BP051",
        "BP052",
        "BP053",
        "BP060",
        "BP061",
        "BP070",
    ]
    assert codes(r, "info") == ["BP012", "BP046"]
    assert len(find(r, "BP020")) == 2  # "22" (no protocol) and 70000
    assert len(find(r, "BP044")) == 2
    assert {f.field for f in find(r, "BP042")} == {
        "directories /srv/drop",
        "files /usr/local/bin/suid",
    }
    assert [f.field for f in find(r, "BP046")] == [
        "files /etc/app/secret.env",
        "files /opt/myapp/conf/x.conf",
    ]
    assert all(f.origin == "Messy" for f in find(r, "BP011"))
    assert r.failed()


def test_policy_and_collisions():
    class Bad(Blueprint):
        files = [File("/usr/lib/x.conf", "x"), File("/etc/a", "a"), File("/etc/a/b", "b")]
        directories = [Directory("/boot/efi/x")]

    r = audit(Bad())
    assert {f.field for f in find(r, "BP040")} == {
        "files /usr/lib/x.conf",
        "directories /boot/efi/x",
    }
    assert [f.message for f in find(r, "BP041")] == ["parent /etc/a is a file, not a directory"]


def test_origin_points_at_declaring_class_and_overlay(tmp_path):
    (tmp_path / "ov/etc").mkdir(parents=True)
    (tmp_path / "ov/etc/creds").write_text("password = supersecret\n")

    class Base(Blueprint):
        overlay = Overlay(tmp_path / "ov")

    class Child(Base):
        users = [User("x", password="plain")]

    r = audit(Child())
    (secret,) = find(r, "BP044")
    assert secret.origin.startswith("Base, overlay ")
    assert find(r, "BP011")[0].origin == "Child"


def test_overlay_findings(tmp_path):
    for name, text in (("lower", "l\n"), ("upper", "u\n")):
        (tmp_path / name / "etc").mkdir(parents=True)
        (tmp_path / name / "etc/motd").write_text(text)
    (tmp_path / "empty").mkdir()
    (tmp_path / "empty/.gitkeep").write_text("")

    class A(Blueprint):
        overlay = [Overlay(tmp_path / "lower"), Overlay(tmp_path / "empty")]

    class B(A):
        overlay = Overlay(tmp_path / "upper")

    class C(B):
        files = [File("/etc/motd", "explicit\n")]

    rb = audit(B())
    assert [f.message for f in find(rb, "BP092")] == ["overlay in B replaces the one from A"]
    assert "contributes no files" in find(rb, "BP091")[0].message
    rc = audit(C())
    assert find(rc, "BP090")[0].message == "an explicit File in C hides the overlay version from B"


def test_ignore_by_class_and_argument():
    class Quiet(Messy):
        AUDIT_IGNORE = ["BP011", "BP046"]

    r = audit(Quiet(), ignore=["BP070"])
    assert not {"BP011", "BP046", "BP070"} & set(codes(r))

    class Quieter(Quiet):  # inherited
        packages = [Remove("telnet-server")]

    assert "BP011" not in codes(audit(Quieter()))


def test_hosted_check():
    class Odd(Clean):
        services = Services(default_target="graphical.target")

    assert codes(audit(Odd())) == []
    assert [f.message for f in find(audit(Odd(), hosted=True), "BP080")] == [
        "not supported by the hosted service: services: 'default_target'"
    ]


def test_report_formats():
    r = audit(Messy())
    text = r.to_text()
    assert text.splitlines()[0] == "messy:"
    assert "error    BP001 hostname: '-bad_host' is not a valid hostname  (Messy)" in text
    assert "hint: use a crypt hash" in text
    assert "BP046" not in r.to_text(show_info=False)
    d = r.to_dict()
    assert d["summary"]["error"] == 5  # BP020 twice
    assert d["findings"][0]["severity"] == "error"


# -- packages & advisories ---------------------------------------------------


def test_load_package_list(tmp_path):
    path = tmp_path / "rpms.txt"
    path.write_text(
        "# rpm -qa --qf '%{NAME} %{EPOCHNUM}:%{VERSION}-%{RELEASE} %{SOURCERPM}\\n'\n"
        "openssl-libs 1:3.0.1-41.el9_0 openssl-3.0.1-41.el9_0.src.rpm\n"
        "bash-5.1.8-9.el9.x86_64\n"
        "gpg-pubkey 0:fd431d51-4ae0493b (none)\n"
    )
    pkgs = load_package_list(path)
    assert [str(p) for p in pkgs] == [
        "openssl-libs-1:3.0.1-41.el9_0",
        "bash-0:5.1.8-9.el9.x86_64",
        "gpg-pubkey-0:fd431d51-4ae0493b",
    ]
    assert pkgs[0].source == "openssl" and pkgs[2].source is None
    path.write_text("???\n")
    with pytest.raises(ValueError, match="can't parse"):
        load_package_list(path)


def test_pinned_packages():
    class Pinned(Blueprint):
        packages = [Package("openssl-libs", "1:3.0.1-41.el9_0"), Package("vim", "9.1*"), "bash"]

    assert [p.evr for p in pinned_packages(Pinned())] == ["1:3.0.1-41.el9_0"]


@pytest.mark.parametrize(
    ("distro", "expected"),
    [
        ("rhel-9", ["Red Hat:enterprise_linux:9::baseos", "Red Hat:enterprise_linux:9::appstream"]),
        (
            "rhel-9.4",
            ["Red Hat:enterprise_linux:9::baseos", "Red Hat:enterprise_linux:9::appstream"],
        ),
        (
            "rhel-810",
            ["Red Hat:enterprise_linux:8::baseos", "Red Hat:enterprise_linux:8::appstream"],
        ),
        ("almalinux-9", ["AlmaLinux:9"]),
        ("rocky-8", ["Rocky Linux:8"]),
        (
            "rhel-10.1",
            ["Red Hat:enterprise_linux:10::baseos", "Red Hat:enterprise_linux:10::appstream"],
        ),
        ("fedora-42", None),
        (None, None),
    ],
)
def test_osv_ecosystems(distro, expected):
    assert osv_ecosystems(distro) == expected


def test_advisories_rhel(osv):
    r = audit(Clean())
    pkgs = [
        ResolvedPackage("openssl-libs", 1, "3.0.1", "41.el9_0", "x86_64"),
        ResolvedPackage("bash", 0, "5.1.8", "9.el9"),
    ]
    check_advisories(r, pkgs, "rhel-9")
    (f,) = find(r, "BP100")
    assert f.message == (
        "RHSA-2023:0946 affects openssl-libs-1:3.0.1-41.el9_0.x86_64: openssl security update"
    )
    assert f.hint == "fixed in 1:3.0.1-47.el9_1"
    assert {q["package"]["ecosystem"] for q in osv.queries} == {
        "Red Hat:enterprise_linux:9::baseos",
        "Red Hat:enterprise_linux:9::appstream",
    }


def test_advisories_alma_uses_source_names(osv):
    r = audit(Clean())
    check_advisories(r, [ResolvedPackage("openssl-libs", 1, "3.0.1", "41.el9_0")], "almalinux-9")
    assert [f.message.split()[0] for f in find(r, "BP100")] == ["ALSA-2023:0946"]
    assert {q["package"]["name"] for q in osv.queries} == {"openssl-libs", "openssl"}


def test_advisories_fixed_version_is_clean(osv):
    r = audit(Clean())
    check_advisories(r, [ResolvedPackage("openssl-libs", 1, "3.0.1", "47.el9_1")], "rhel-9")
    assert find(r, "BP100") == []


def test_advisories_unsupported_and_unavailable(osv):
    r = audit(Clean())
    check_advisories(r, [], "fedora-42")
    assert codes(r) == ["BP102"]
    r = audit(Clean())
    check_advisories(r, [], "rhel-9")
    assert codes(r) == ["BP101"]
    osv.down = True
    r = audit(Clean())
    check_advisories(r, [ResolvedPackage("bash", 0, "5", "1")], "rhel-9")
    assert codes(r) == ["BP103"]


# -- CLI ---------------------------------------------------------------------


def test_cli_audit_module_and_exit_codes(capsys):
    assert main(["audit", "examples/fleet.py"]) == 0
    out = capsys.readouterr().out
    assert "staging-web:" in out and "BP010 users dev" in out
    assert main(["audit", "examples/fleet.py:StagingWeb", "--strict"]) == 1
    capsys.readouterr()
    assert main(["audit", "examples/fleet.py:StagingWeb", "--strict", "--ignore", "BP010"]) == 0
    capsys.readouterr()
    assert main(["audit", "examples/fleet.py:WebServer", "--json", "-q"]) == 0
    (report,) = json.loads(capsys.readouterr().out)
    assert report["blueprint"] == "web-server"


def test_cli_audit_errors_fail(tmp_path, capsys):
    mod = tmp_path / "bad.py"
    mod.write_text("from bpkit import Blueprint\nclass Bad(Blueprint):\n    hostname = 'no_way'\n")
    assert main(["audit", str(mod)]) == 1
    assert "BP001" in capsys.readouterr().out


def test_cli_advisories_with_depsolve(composer, osv, capsys):
    rc = main(
        [
            "audit",
            "examples/fleet.py:WebServer",
            "--advisories",
            "--depsolve",
            "composer",
            "--socket",
            composer.socket,
            "--json",
        ]
    )
    assert rc == 0
    (report,) = json.loads(capsys.readouterr().out)
    assert [f["code"] for f in report["findings"] if f["code"] == "BP100"] == ["BP100"]
    assert "openssl-libs-1:3.0.1-41.el9_0.x86_64" in report["packages"]
    assert "web-server" in composer.blueprints


def test_cli_advisories_from_package_list(osv, tmp_path, capsys):
    rpms = tmp_path / "rpms.txt"
    rpms.write_text("openssl-libs 1:3.0.1-41.el9_0 openssl-3.0.1-41.el9_0.src.rpm\n")
    assert (
        main(
            [
                "audit",
                "examples/fleet.py:WebServer",
                "--advisories",
                "--packages",
                str(rpms),
                "--strict",
            ]
        )
        == 1
    )
    assert "RHSA-2023:0946" in capsys.readouterr().out


def test_class_ignore_covers_advisories(osv):
    class Quiet(Clean):
        distro = "fedora-42"
        AUDIT_IGNORE = ["BP102"]

    r = audit(Quiet())
    check_advisories(r, [], "fedora-42")
    assert codes(r) == []


def test_depsolve_errors_raise(composer):
    from bpkit.audit import resolve_with_composer
    from bpkit.composer import ComposerClient

    composer.depsolve_errors = [{"id": "BlueprintsError", "msg": "clean: no package nope"}]
    with pytest.raises(RuntimeError, match="no package nope"):
        resolve_with_composer(Clean(), ComposerClient(composer.socket))
