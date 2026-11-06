"""bpkit: image-builder blueprints as Python classes with inheritance."""

from .blueprint import Blueprint, BlueprintError, kebab_case
from .markers import Remove, Replace, Unset
from .merge import KeyedList, MergeError, StrSet
from .models import (
    AnacondaModules,
    Container,
    Directory,
    EnabledModule,
    File,
    Filesystem,
    Firewall,
    FirewallServices,
    FirewallZone,
    Group,
    Installer,
    Kernel,
    Kickstart,
    Locale,
    Package,
    PackageGroup,
    Repository,
    Services,
    SSHKey,
    User,
)
from .overlay import Overlay, OverlayError

__version__ = "0.1.0"

__all__ = [
    "AnacondaModules",
    "Blueprint",
    "BlueprintError",
    "Container",
    "Directory",
    "EnabledModule",
    "File",
    "Filesystem",
    "Firewall",
    "FirewallServices",
    "FirewallZone",
    "Group",
    "Installer",
    "Kernel",
    "KeyedList",
    "Kickstart",
    "Locale",
    "MergeError",
    "Overlay",
    "OverlayError",
    "Package",
    "PackageGroup",
    "Remove",
    "Replace",
    "Repository",
    "SSHKey",
    "Services",
    "StrSet",
    "Unset",
    "User",
    "kebab_case",
]
