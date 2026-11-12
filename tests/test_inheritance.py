"""Resolution and merge rules across class hierarchies."""

import pytest

from bpkit import (
    Blueprint,
    BlueprintError,
    Filesystem,
    Firewall,
    FirewallServices,
    Locale,
    Package,
    Remove,
    Replace,
    Services,
    Unset,
    User,
)


def pkgs(bp: Blueprint) -> list[str]:
    return [p.name for p in bp.packages]


# -- single inheritance -------------------------------------------------------


class Base(Blueprint):
    """Common base for all our images.

    More detail that isn't part of the description.
    """

    packages = ["vim", "tmux"]
    services = Services(enabled=["sshd"])
    timezone = "Australia/Sydney"
    hostname = "base"
    users = [User(name="ops", groups=["wheel"], key="ssh-ed25519 AAAA")]


class Child(Base):
    packages = ["nginx"]
    timezone = "UTC"


def test_lists_add_scalars_override():
    bp = Child()
    assert pkgs(bp) == ["vim", "tmux", "nginx"]
    assert bp.timezone == "UTC"
    assert bp.hostname == "base"  # inherited untouched


def test_shorthand_packages_are_normalised():
    bp = Child()
    assert bp.packages["vim"] == Package(name="vim", version="*")


def test_keyed_child_entry_replaces_parent_entry_in_place():
    class Pinned(Base):
        packages = [Package("tmux", "3.4*"), "htop"]

    bp = Pinned()
    assert pkgs(bp) == ["vim", "tmux", "htop"]  # first-seen order kept
    assert bp.packages["tmux"].version == "3.4*"


def test_keyed_user_replacement():
    class Zsh(Base):
        users = [User("ops", shell="/bin/zsh")]

    (ops,) = Zsh().users
    assert ops.shell == "/bin/zsh"
    assert ops.groups is None  # whole entry replaced, not field-merged


# -- multi-level --------------------------------------------------------------


class L1(Blueprint):
    packages = ["a"]
    hostname = "l1"
    kernel_append = ["quiet"]


class L2(L1):
    packages = ["b"]
    kernel_append = ["audit=1"]


class L3(L2):
    packages = ["c", Remove("a")]
    hostname = "l3"


class L4(L3):
    packages = ["d"]
    kernel_append = ["quiet", "console=ttyS0"]


def test_multi_level_stacking():
    bp = L4()
    assert pkgs(bp) == ["b", "c", "d"]
    assert bp.hostname == "l3"
    assert list(bp.kernel_append) == ["quiet", "audit=1", "console=ttyS0"]
    assert bp.to_dict()["customizations"]["kernel"]["append"] == "quiet audit=1 console=ttyS0"


def test_kernel_append_list_items_split_like_the_string_form():
    class K(Blueprint):
        kernel_append = ["audit=1 quiet"]

    class K2(K):
        kernel_append = [Remove("quiet")]

    assert list(K2().kernel_append) == ["audit=1"]


# -- mixins and diamonds -----------------------------------------------------


class Hardened(Blueprint, abstract=True):
    services = Services(enabled=["firewalld"], masked=["cups"])
    kernel_append = ["audit=1"]
    timezone = "Etc/Hardened"
    packages = ["aide"]


class WebServer(Base, Hardened):
    packages = ["nginx"]
    filesystem = [Filesystem("/var", "10 GiB")]


def test_mixin_merge_follows_mro():
    bp = WebServer()
    # reversed MRO: Hardened applies before Base, so Base (leftmost) wins scalars
    assert bp.timezone == "Australia/Sydney"
    assert pkgs(bp) == ["aide", "vim", "tmux", "nginx"]
    assert bp.services.enabled == ["firewalld", "sshd"]
    assert bp.services.masked == ["cups"]
    assert list(bp.kernel_append) == ["audit=1"]


def test_diamond():
    class Root(Blueprint):
        packages = ["root"]
        hostname = "root"
        locale = Locale(languages=["en_US.UTF-8"])

    class Left(Root):
        packages = ["left"]
        hostname = "left"

    class Right(Root):
        packages = ["right", Remove("root")]
        hostname = "right"
        locale = Locale(keyboard="de")

    class Bottom(Left, Right):
        packages = ["bottom"]

    assert [c.__name__ for c in Bottom.__mro__[:4]] == ["Bottom", "Left", "Right", "Root"]
    bp = Bottom()
    # Root applied once, then Right, Left, Bottom
    assert pkgs(bp) == ["right", "left", "bottom"]
    assert bp.hostname == "left"  # matches Python attribute lookup
    assert bp.hostname == Bottom.hostname
    assert bp.locale.languages == ["en_US.UTF-8"]
    assert bp.locale.keyboard == "de"


# -- markers -----------------------------------------------------------------


def test_remove_drops_inherited_entry():
    class Staging(WebServer):
        packages = ["htop", Remove("tmux")]

    assert pkgs(Staging()) == ["aide", "vim", "nginx", "htop"]


def test_remove_multiple_keys_and_other_kinds():
    class NoOps(WebServer):
        users = [Remove("ops")]
        filesystem = [Remove("/var")]
        kernel_append = [Remove("audit=1")]
        services = Services(masked=[Remove("cups")])

    bp = NoOps()
    assert len(bp.users) == 0
    assert len(bp.filesystem) == 0
    assert list(bp.kernel_append) == []
    assert bp.services.masked == []
    assert "customizations" not in bp.to_dict() or "user" not in bp.to_dict()["customizations"]


def test_remove_missing_key_raises_at_class_definition():
    with pytest.raises(BlueprintError, match=r"Remove\('tmuxx'\).*inherited: 'vim', 'tmux'"):

        class Typo(Base):
            packages = [Remove("tmuxx")]


def test_remove_missing_set_item_raises():
    with pytest.raises(BlueprintError, match="cannot Remove"):

        class Typo(Base):
            services = Services(enabled=[Remove("sshdd")])


def test_abstract_mixin_may_remove_and_is_checked_when_used():
    class NoTmux(Blueprint, abstract=True):
        packages = [Remove("tmux")]

    class Web(NoTmux, Base):
        pass

    assert pkgs(Web()) == ["vim"]

    with pytest.raises(BlueprintError, match="while resolving Bad"):

        class Bad(NoTmux):  # nothing to remove tmux from
            pass


def test_replace_discards_inherited():
    class Minimal(WebServer):
        packages = Replace(["nginx"])
        services = Replace(Services(enabled=["nginx"]))

    bp = Minimal()
    assert pkgs(bp) == ["nginx"]
    assert bp.services.enabled == ["nginx"]
    assert bp.services.masked == []


def test_replace_inside_struct_field():
    class OnlyMine(WebServer):
        services = Services(enabled=Replace(["nginx"]))

    bp = OnlyMine()
    assert bp.services.enabled == ["nginx"]
    assert bp.services.masked == ["cups"]


def test_unset_removes_inherited_scalar():
    class NoTz(WebServer):
        timezone = Unset
        hostname = Unset

    bp = NoTz()
    assert bp.timezone is None
    customizations = bp.to_dict()["customizations"]
    assert "timezone" not in customizations
    assert "hostname" not in customizations


def test_unset_struct_and_list():
    class Bare(WebServer):
        services = Unset
        packages = Unset

    d = Bare().to_dict()
    assert "packages" not in d
    assert "services" not in d["customizations"]


def test_none_is_rejected_for_scalars():
    with pytest.raises(BlueprintError, match="use Unset"):

        class Bad(Base):
            hostname = None


# -- services conflict resolution -------------------------------------------


def test_child_enabling_masked_service_moves_it():
    class Printer(WebServer):
        services = Services(enabled=["cups"])

    bp = Printer()
    assert "cups" in bp.services.enabled
    assert bp.services.masked == []


def test_child_disabling_enabled_service_moves_it():
    class NoSsh(WebServer):
        services = Services(disabled=["sshd"], masked=["firewalld"])

    s = NoSsh().services
    assert s.enabled == []
    assert s.disabled == ["sshd"]
    assert s.masked == ["cups", "firewalld"]


def test_same_service_in_two_lists_of_one_declaration_raises():
    with pytest.raises(ValueError, match="both 'enabled' and 'masked'"):
        Services(enabled=["x"], masked=["x"])


def test_firewall_services_conflicts_and_nesting():
    class A(Blueprint):
        firewall = Firewall(
            ports=["22:tcp"], services=FirewallServices(enabled=["ssh"], disabled=["cockpit"])
        )

    class B(A):
        firewall = Firewall(ports=["80:tcp"], services=FirewallServices(enabled=["cockpit"]))

    fw = B().to_dict()["customizations"]["firewall"]
    assert fw == {"ports": ["22:tcp", "80:tcp"], "services": {"enabled": ["ssh", "cockpit"]}}


# -- kernel ------------------------------------------------------------------


def test_kernel_append_dedupes_and_joins():
    class A(Blueprint):
        kernel_append = "audit=1 quiet"

    class B(A):
        kernel_append = ["quiet", "nosmt", "nosmt"]

    d = B().to_dict()
    assert d["customizations"]["kernel"] == {"append": "audit=1 quiet nosmt"}


def test_kernel_name_and_append_share_a_table():
    from bpkit import Kernel

    class A(Blueprint):
        kernel = Kernel("kernel-debug")
        kernel_append = ["debug"]

    assert A().to_dict()["customizations"]["kernel"] == {"name": "kernel-debug", "append": "debug"}


# -- identity ----------------------------------------------------------------


def test_name_and_description_not_inherited():
    class StagingWeb(WebServer):
        pass

    bp = StagingWeb()
    assert bp.name == "staging-web"
    assert bp.description is None  # WebServer/Base docstrings are not inherited
    assert Base().description == "Common base for all our images."
    assert Base().name == "base"


def test_explicit_identity_and_per_class_version():
    class A(Blueprint):
        name = "custom"
        description = "explicit"
        version = "1.2.3"

    class B(A):
        pass

    a, b = A(), B()
    assert (a.name, a.description, a.version) == ("custom", "explicit", "1.2.3")
    assert (b.name, b.description, b.version) == ("b", None, "0.0.1")


@pytest.mark.parametrize(
    ("cls", "expected"),
    [
        ("StagingWeb", "staging-web"),
        ("HTTPServer", "http-server"),
        ("Rhel9Base", "rhel9-base"),
        ("web_server", "web-server"),
        ("A", "a"),
    ],
)
def test_kebab_case(cls, expected):
    from bpkit import kebab_case

    assert kebab_case(cls) == expected


# -- declaration errors ------------------------------------------------------


def test_unknown_field_is_an_error_with_suggestion():
    with pytest.raises(BlueprintError, match="did you mean 'packages'"):

        class Typo(Blueprint):
            pakages = ["vim"]


def test_own_attributes_allowed_if_private_or_constant():
    class Mine(Blueprint):
        REGION = "au"
        _cache = {}

        def helper(self):
            return 1

        @property
        def thing(self):
            return 2

    assert Mine().helper() == 1


def test_string_instead_of_list_is_an_error():
    with pytest.raises(BlueprintError, match="expected a list"):

        class Bad(Blueprint):
            packages = "vim"


def test_duplicate_key_in_one_declaration_is_an_error():
    with pytest.raises(BlueprintError, match="duplicate name 'vim'"):

        class Bad(Blueprint):
            packages = ["vim", Package("vim", "9*")]


def test_validation_errors_surface_at_definition():
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc:

        class Bad(Blueprint):
            filesystem = [Filesystem("var", "10 GiB")]

    assert "absolute" in str(exc.value)


def test_invalid_scalar_type():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):

        class Bad(Blueprint):
            fips = "yes"

    with pytest.raises(ValidationError):

        class Bad2(Blueprint):
            partitioning_mode = "zfs"
