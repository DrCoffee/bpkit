"""CycloneDX SBOMs of the packages an image installs.

::

    bpkit sbom fleet.py:WebServer -t qcow2                   # CycloneDX JSON to stdout
    bpkit sbom fleet.py:WebServer --format xml -o web.cdx.xml
    bpkit sbom fleet.py:WebServer --repo http://mirror.lan/fedora/42/x86_64/os/
    bpkit sbom fleet.py:WebServer --backend composer         # depsolve via osbuild-composer

By default the package set comes from ``image-builder manifest`` (no build,
no root), so it reflects the repositories the build would really use,
including ``--repo``/``BUILD_REPOS`` overrides (see `bpkit.build`). Each RPM
becomes a ``library`` component with a ``pkg:rpm`` purl and, from
image-builder, its SHA-256. ``--backend composer`` uses osbuild-composer's
depsolve instead, which knows epochs but not file digests.

From Python::

    from bpkit.sbom import cyclonedx, packages_from_manifest
    from bpkit.build import manifest

    pkgs = packages_from_manifest(manifest(WebServer(), "qcow2"))
    print(cyclonedx(pkgs, name="web-server", distro="fedora-42", fmt="xml"))
"""

from __future__ import annotations

import json
import re
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as dist_version
from pathlib import PurePosixPath
from typing import Any, Literal
from urllib.parse import quote

from .audit import _NEVRA, ResolvedPackage

__all__ = ["SPEC_VERSION", "cyclonedx", "packages_from_manifest", "purl"]

SPEC_VERSION = "1.6"
_XMLNS = f"http://cyclonedx.org/schema/bom/{SPEC_VERSION}"
# purl namespaces (vendors) by distro prefix
_VENDORS = {
    "fedora": "fedora",
    "rhel": "redhat",
    "centos": "centos",
    "almalinux": "almalinux",
    "alma": "almalinux",
    "rocky": "rocky",
}


def packages_from_manifest(manifest: str | dict[str, Any]) -> list[ResolvedPackage]:
    """The packages installed into the image by an osbuild manifest.

    Uses the ``os`` pipeline's rpm stages (falling back to every pipeline but
    ``build``, whose packages only exist in the build root). Epochs aren't in
    the manifest, so they are reported as 0.
    """
    m = json.loads(manifest) if isinstance(manifest, str) else manifest
    paths: dict[str, str] = {}
    for source in m.get("sources", {}).values():
        for digest, item in source.get("items", {}).items():
            # librepo items are {"path": ...}; curl items are a URL or {"url": ...}
            ref = item if isinstance(item, str) else item.get("path") or item.get("url", "")
            paths[digest] = ref

    pipelines = m.get("pipelines", [])
    chosen = [p for p in pipelines if p.get("name") == "os"] or [
        p for p in pipelines if p.get("name") != "build"
    ]
    out: dict[str, ResolvedPackage] = {}
    for pipeline in chosen:
        for stage in pipeline.get("stages", []):
            if stage.get("type") != "org.osbuild.rpm":
                continue
            for ref in stage.get("inputs", {}).get("packages", {}).get("references", []):
                digest = ref["id"] if isinstance(ref, dict) else ref
                filename = PurePosixPath(paths.get(digest, "")).name
                nevra = _NEVRA.match(filename)
                if not nevra or not filename.endswith(".rpm"):
                    raise ValueError(f"can't read a package name from {filename!r} ({digest})")
                out[digest] = ResolvedPackage(
                    nevra["name"],
                    0,
                    nevra["version"],
                    nevra["release"],
                    nevra["arch"],
                    sha256=digest.removeprefix("sha256:") if digest.startswith("sha256:") else None,
                )
    if not out:
        raise ValueError("manifest has no rpm stages outside the build pipeline")
    return sorted(out.values(), key=lambda p: (p.name, p.arch or "", p.evr))


def purl(pkg: ResolvedPackage, distro: str | None = None) -> str:
    """``pkg:rpm/<vendor>/<name>@<version>-<release>?arch=…&epoch=…&distro=…``."""
    vendor = None
    if distro and (m := re.match(r"[a-z]+", distro)):
        vendor = _VENDORS.get(m.group())
    ns = f"{vendor}/" if vendor else ""
    quals = []
    if pkg.arch:
        quals.append(f"arch={pkg.arch}")
    if pkg.epoch:
        quals.append(f"epoch={pkg.epoch}")
    if distro:
        quals.append(f"distro={quote(distro, safe='')}")
    q = f"?{'&'.join(quals)}" if quals else ""
    vr = quote(f"{pkg.version}-{pkg.release}", safe="")
    return f"pkg:rpm/{ns}{quote(pkg.name, safe='')}@{vr}{q}"


def _bpkit_version() -> str:
    try:
        return dist_version("osbuild-bpkit")
    except PackageNotFoundError:  # pragma: no cover - running from a plain checkout
        return "unknown"


def _bom(
    packages: Sequence[ResolvedPackage], name: str, version: str | None, distro: str | None
) -> dict[str, Any]:
    subject: dict[str, Any] = {"type": "operating-system", "bom-ref": "image", "name": name}
    if version:
        subject["version"] = version
    components = []
    for p in packages:
        c: dict[str, Any] = {
            "type": "library",
            "bom-ref": purl(p, distro),
            "name": p.name,
            "version": (f"{p.epoch}:" if p.epoch else "") + f"{p.version}-{p.release}",
            "purl": purl(p, distro),
        }
        if p.sha256:
            c["hashes"] = [{"alg": "SHA-256", "content": p.sha256}]
        components.append(c)
    return {
        "bomFormat": "CycloneDX",
        "specVersion": SPEC_VERSION,
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tools": {
                "components": [
                    {"type": "application", "name": "bpkit", "version": _bpkit_version()}
                ]
            },
            "component": subject,
        },
        "components": components,
        "dependencies": [{"ref": "image", "dependsOn": [c["bom-ref"] for c in components]}],
    }


def _xml(bom: dict[str, Any]) -> str:
    ET.register_namespace("", _XMLNS)

    def el(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
        e = ET.SubElement(parent, f"{{{_XMLNS}}}{tag}", attrs)
        e.text = text
        return e

    def component(parent: ET.Element, c: dict[str, Any]) -> None:
        attrs = {"type": c["type"]}
        if "bom-ref" in c:
            attrs["bom-ref"] = c["bom-ref"]
        e = el(parent, "component", **attrs)
        el(e, "name", c["name"])  # schema order: name, version, hashes, purl
        if "version" in c:
            el(e, "version", c["version"])
        if c.get("hashes"):
            hashes = el(e, "hashes")
            for h in c["hashes"]:
                el(hashes, "hash", h["content"], alg=h["alg"])
        if "purl" in c:
            el(e, "purl", c["purl"])

    root = ET.Element(
        f"{{{_XMLNS}}}bom", {"serialNumber": bom["serialNumber"], "version": str(bom["version"])}
    )
    meta = el(root, "metadata")
    el(meta, "timestamp", bom["metadata"]["timestamp"])
    tools = el(el(meta, "tools"), "components")
    for t in bom["metadata"]["tools"]["components"]:
        component(tools, t)
    component(meta, bom["metadata"]["component"])
    comps = el(root, "components")
    for c in bom["components"]:
        component(comps, c)
    deps = el(root, "dependencies")
    for d in bom["dependencies"]:
        dep = el(deps, "dependency", ref=d["ref"])
        for ref in d["dependsOn"]:
            el(dep, "dependency", ref=ref)
    ET.indent(root)
    return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"


def cyclonedx(
    packages: Sequence[ResolvedPackage],
    *,
    name: str,
    version: str | None = None,
    distro: str | None = None,
    fmt: Literal["json", "xml"] = "json",
) -> str:
    """A CycloneDX 1.6 BOM (JSON or XML) listing ``packages`` as parts of image ``name``."""
    bom = _bom(packages, name, version, distro)
    if fmt == "xml":
        return _xml(bom)
    if fmt != "json":
        raise ValueError(f"format must be 'json' or 'xml', not {fmt!r}")
    return json.dumps(bom, indent=2) + "\n"
