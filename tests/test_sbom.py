"""CycloneDX output and build-repo overrides (no image-builder needed)."""

import json
import xml.etree.ElementTree as ET

import pytest

from bpkit import Blueprint, Package
from bpkit.build import _repo_args, image_builder_command
from bpkit.cli import main
from bpkit.sbom import cyclonedx, packages_from_manifest, purl

MANIFEST = {
    "pipelines": [
        {"name": "build", "stages": [{"type": "org.osbuild.rpm", "inputs": {"packages": {
            "references": [{"id": "sha256:bb"}]}}}]},
        {"name": "os", "stages": [{"type": "org.osbuild.rpm", "inputs": {"packages": {
            "references": [{"id": "sha256:aa"}, {"id": "sha256:cc"}]}}}]},
    ],
    "sources": {"org.osbuild.librepo": {"items": {
        "sha256:aa": {"path": "Packages/t/tmux-3.5a-4.fc42.x86_64.rpm"},
        "sha256:bb": {"path": "Packages/g/gcc-15.0-1.fc42.x86_64.rpm"},
        "sha256:cc": {"path": "Packages/b/bash-completion-2.16-1.fc42.noarch.rpm"},
    }}},
}  # fmt: skip


class Mirrored(Blueprint, abstract=True):
    BUILD_REPOS = ["file:///srv/mirror/os/"]


class App(Mirrored):
    distro = "fedora-42"
    packages = [Package("tmux")]


def test_manifest_packages_skip_build_root():
    pkgs = packages_from_manifest(json.dumps(MANIFEST))
    assert [(p.name, p.version, p.release, p.arch, p.sha256) for p in pkgs] == [
        ("bash-completion", "2.16", "1.fc42", "noarch", "cc"),
        ("tmux", "3.5a", "4.fc42", "x86_64", "aa"),
    ]
    assert (
        purl(pkgs[1], "fedora-42") == "pkg:rpm/fedora/tmux@3.5a-4.fc42?arch=x86_64&distro=fedora-42"
    )


def test_cyclonedx_json_and_xml_agree():
    pkgs = packages_from_manifest(MANIFEST)
    bom = json.loads(cyclonedx(pkgs, name="app", version="1.0", distro="fedora-42"))
    assert bom["bomFormat"] == "CycloneDX" and bom["specVersion"] == "1.6"
    assert [c["name"] for c in bom["components"]] == ["bash-completion", "tmux"]
    assert bom["components"][1]["hashes"] == [{"alg": "SHA-256", "content": "aa"}]

    ns = {"c": "http://cyclonedx.org/schema/bom/1.6"}
    root = ET.fromstring(cyclonedx(pkgs, name="app", distro="fedora-42", fmt="xml"))
    purls = [e.text for e in root.findall("c:components/c:component/c:purl", ns)]
    assert purls == [c["purl"] for c in bom["components"]]
    with pytest.raises(ValueError):
        cyclonedx(pkgs, name="app", fmt="yaml")


def test_build_repos_from_class_and_override():
    assert _repo_args(App(), None, None, None)["repos"] == ["file:///srv/mirror/os/"]
    assert _repo_args(App(), ["http://m/"], None, None)["repos"] == ["http://m/"]
    cmd = image_builder_command(
        "manifest", "qcow2", "bp.toml", repos=["http://a/", "http://b/"], extra_repos=["http://x/"]
    )
    assert cmd[5:] == [
        "--force-repo", "http://a/", "--force-repo", "http://b/", "--extra-repo", "http://x/",
    ]  # fmt: skip


def test_repo_flags_rejected_for_remote_backends(capsys):
    assert main(["sbom", f"{__file__}:App", "--backend", "composer", "--repo", "http://m/"]) == 2
    assert "only apply to --backend local" in capsys.readouterr().err
