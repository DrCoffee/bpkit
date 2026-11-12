"""
.. include:: ../../docs/guide.md
   :end-before: <!-- page: basics -->

## Guide

1. [Writing blueprints](guide/basics.html): classes, inheritance, removing and replacing values
2. [Overlays](guide/overlays.html): config files from a directory
3. [Instances and debugging](guide/instances.html): kwargs, `customize()`, `explain()`
4. [CLI, import and builds](guide/cli.html): render, diff, import, build images
5. [Auditing and SBOMs](guide/audit.html): checks, advisories, CycloneDX output
6. [Gotchas](guide/gotchas.html)

## API

`Blueprint` is the base class and the models (`Package`, `User`, `File`,
`Services`, …) are importable from `bpkit`. Each module has its own page:
`bpkit.blueprint`, `bpkit.models` (every field), `bpkit.overlay`, `bpkit.markers`,
`bpkit.merge`, `bpkit.render`, `bpkit.cli`, `bpkit.importer`, `bpkit.build`,
`bpkit.composer`, `bpkit.hosted`, `bpkit.audit` and `bpkit.sbom`.
"""

from . import audit, build, cli, composer, hosted, importer, markers, models, overlay, sbom
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

__version__ = sbom._bpkit_version()  # from the installed distribution metadata
