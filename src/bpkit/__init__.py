"""
.. include:: ../../docs/guide.md

## API

`Blueprint` is the base class. The models (`Package`, `User`, `File`,
`Services`, …) are re-exported here; see `bpkit.models` for every field.
`bpkit.overlay` covers overlay options and `bpkit.cli` the commands.
"""

from . import cli, markers, models, overlay
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

# Submodules are listed so pdoc documents them.
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
    "cli",
    "kebab_case",
    "markers",
    "models",
    "overlay",
]
