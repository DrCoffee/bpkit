Write image-builder blueprints as Python classes. Subclasses inherit and extend
their parents, and bpkit renders each class to blueprint TOML for
`image-builder` or `composer-cli`.

```
pip install bpkit            # or: pip install -e ".[dev]" from a checkout
```

## 1. A first blueprint

```python
from bpkit import Blueprint, Services, User

class Base(Blueprint):
    """Common base for all our images."""
    distro = "rhel-9"
    packages = ["vim-enhanced", "tmux"]
    services = Services(enabled=["sshd"])
    timezone = "UTC"
    users = [User("ops", groups=["wheel"], key="ssh-ed25519 AAAA...")]

print(Base().to_toml())
```

```toml
name = "base"
description = "Common base for all our images."
version = "0.0.1"
distro = "rhel-9"
packages = [
    { name = "vim-enhanced", version = "*" },
    { name = "tmux", version = "*" },
]
# ...
```

`name` is the kebab-cased class name and `description` is the first line of
the docstring. Neither is inherited, and neither is `version`. Set any of
them explicitly to override.

Python names match the blueprint's, with a few renames:
`users` → `customizations.user`, `user_groups` → `customizations.group`,
`sshkeys` → `customizations.sshkey`. `kernel_append` is a list that renders as
`customizations.kernel.append`. Bare strings are fine anywhere an entry has
an obvious key, e.g. `"vim"` → `Package("vim", "*")`.

## 2. Inheritance

```python
from bpkit import Filesystem

class Hardened(Blueprint, abstract=True):        # mixin; bulk render skips it
    packages = ["aide"]
    services = Services(enabled=["firewalld"], masked=["cups"])
    kernel_append = ["audit=1"]

class WebServer(Base, Hardened):
    packages = ["nginx"]                          # added to the inherited list
    timezone = "Australia/Sydney"                 # replaces the inherited value
    filesystem = [Filesystem("/var", "10 GiB")]

web = WebServer()
assert [p.name for p in web.packages] == ["aide", "vim-enhanced", "tmux", "nginx"]
assert web.services.enabled == ["firewalld", "sshd"]
```

How each kind of field merges:

| Field kind | Examples | On inheritance |
|---|---|---|
| scalar | `hostname`, `timezone`, `fips`, `distro` | child replaces |
| keyed list | `packages`, `users`, `files`, `filesystem`, `repositories` | union by key (`name`, `path`, `mountpoint`, `id`…); a same-key child entry replaces the parent's in place |
| string set | `kernel_append`, `ntpservers` | ordered union, deduped |
| struct | `services`, `firewall`, `locale`, `kernel`, `installer` | field by field, using the rules above |

Order follows the MRO. Each class's own body is applied once, starting from the
far end of the MRO. For scalars the leftmost base therefore wins, exactly as
with plain Python attribute lookup. In lists, mixin entries come before the
base's (`aide` above).

Services are exclusive: if a child enables a service a parent masked, the
service moves from `masked` to `enabled`. It is never listed in both.

## 3. Removing and replacing inherited values

```python
from bpkit import Remove, Replace, Unset

class Staging(WebServer):
    packages = ["htop", Remove("tmux")]           # drop one inherited entry
    services = Services(masked=[Remove("cups")])  # works inside structs too
    timezone = Unset                              # drop an inherited scalar

class Minimal(WebServer):
    packages = Replace(["nginx"])                 # discard everything inherited

assert "tmux" not in Staging().packages
assert [p.name for p in Minimal().packages] == ["nginx"]
```

`Remove("tmuxx")` raises when the class is defined, because nothing called
`tmuxx` was inherited. Typos fail on import, not in a build. The exception is
an `abstract=True` mixin: it can remove things it doesn't inherit, and the
check runs when a concrete class uses it.

## 4. Config files from a directory (overlays)

Lay files out as they should appear on the image:

```
overlays/web/
├── etc/nginx/conf.d/site.conf
├── etc/myapp/.gitkeep          # keeps an empty directory
└── usr/local/bin/healthcheck   # 0755 on disk → mode "0755"
```

```python
from bpkit import File, Overlay

class Web(Base):
    overlay = Overlay("overlays/web")          # relative to this .py file

class WebDebug(Web):
    overlay = "overlays/debug"                 # a plain path works too
    files = [File("/etc/motd", "debug build\n")]

assert "/etc/nginx/conf.d/site.conf" in Web().files
```

What an overlay generates:

- **Files:** one `files` entry per file, with the contents inline and the
  mode taken from disk.
- **Directories:** one `directories` entry per leaf directory, with
  `ensure_parents = true` and no mode or owner. image-builder therefore
  tolerates directories the distro already ships (`/etc/nginx`).

The entries merge by path like any other files: a child's overlay file
replaces the parent's.

Dropping an inherited overlay works like removing a layer from an overlayfs
stack. Whatever that overlay was covering comes back:

```python
class NoDebug(WebDebug):
    overlay = [Remove("overlays/debug")]       # just that overlay

class NoOverlays(WebDebug):
    overlay = Unset                            # every inherited overlay
                                               # (explicit File entries stay)

class Swap(WebDebug):
    overlay = Replace(Overlay("overlays/other"))  # drop them all, add this one

assert "/etc/myapp" not in NoOverlays().directories
```

Symlinks, binary files, and paths that image-builder refuses (`/usr` outside
`/usr/local`, `/boot`, `/etc/passwd`) raise when the class is defined.
Options: `prefix="/etc/app"`, `user=`, `group=`, `file_mode=`, `dir_mode=`,
`directories="all"|"none"`, `exclude=[...]`, `check_policy=False`.

## 5. Instances: kwargs and `customize()`

Constructor kwargs merge the same way a subclass would:

```python
web01 = WebServer(hostname="web01", packages=["curl"])
assert "curl" in web01.packages and web01.hostname == "web01"
```

For logic (env vars, conditionals), override `customize()`. It runs on the
instance after resolution and kwargs. Call `super()` so mixins' hooks run too:

```python
import os

class Monitored(Blueprint, abstract=True):
    def customize(self) -> None:
        super().customize()
        self.packages.add("node-exporter")
        self.services.enable("node-exporter")

class App(Monitored, WebServer):
    def customize(self) -> None:
        super().customize()
        if os.environ.get("IMAGE_DEBUG"):
            self.packages.add("gdb")
        self.packages.remove("tmux")           # KeyError if it isn't there
        self.hostname = f"app-{self.version}"  # assignment replaces the value
```

Helpers:

- Keyed lists: `add` (replaces an entry with the same key), `remove`,
  `discard`, `get`, `in`, and lookup by key (`self.users["ops"]`).
- `Services`: `enable`, `disable`, `mask`, `reset`.
- `Firewall`: `open_port`, `close_port`.

Each instance works on its own deep copy, so changes never leak into the
class.

## 6. Debugging a hierarchy

```python
print(App().explain("packages"))
```

```
packages:
  aide            <- Hardened
  vim-enhanced    <- Base
  nginx           <- WebServer
  node-exporter   <- customize()
```

`explain()` with no field covers everything. It works on a class too
(`App.explain()`), which skips kwargs and `customize()`.

## 7. CLI

Targets are `module.path:Class` or `path/to/file.py:Class`.

```
bpkit list   blueprints.py                     # classes, bases, abstract or not
bpkit render blueprints.py:WebServer           # TOML to stdout (-o FILE)
bpkit render blueprints.py -o out/             # every concrete class → out/<name>.toml
bpkit diff   blueprints.py:WebServer blueprints.py:Minimal   # exit 1 if they differ
bpkit explain blueprints.py:App packages
bpkit overlay overlays/web                     # preview what a directory generates
```

Output is deterministic, so you can commit the rendered TOML and review the
diffs, or render it in CI and pass it straight on:

```
bpkit render blueprints.py -o out/
image-builder build qcow2 --blueprint out/web-server.toml
```

## 8. Anything not modelled yet

`extra_customizations` is deep-merged into `[customizations]` (and inherits
the same way), so a gap in bpkit never blocks you:

```python
class Cis(WebServer):
    extra_customizations = {"openscap": {"profile_id": "xccdf_org.ssgproject.content_profile_cis"}}
```

## 9. Importing existing blueprints

```
bpkit import web.toml db.toml --base Fleet -o fleet.py --overlay-dir overlays
```

You get one class per blueprint. `--base Fleet` moves whatever every
blueprint shares into an abstract `Fleet`, and each subclass keeps only its
differences. `--overlay-dir` writes inline file contents to
`overlays/<name>/` and uses `Overlay(...)` in place of `File(...)`.
`--from-composer NAME` imports a blueprint stored in osbuild-composer.

The generated classes render back to the same blueprints. Only list order
can change, because the base's entries come first.

## 10. Building images

```
bpkit build fleet.py:WebServer -t qcow2                       # local image-builder
bpkit build fleet.py:WebServer -t qcow2 --backend composer    # osbuild-composer
bpkit build fleet.py:WebServer -t guest-image --backend hosted  # console.redhat.com
bpkit push  fleet.py:WebServer                                # store in composer
bpkit status <compose-id> --backend hosted
```

- **local** runs `image-builder build --blueprint …`. Extra flags go
  after `--`.
- **composer** pushes the blueprint over `/run/weldr/api.socket`, composes,
  waits, and downloads into `-o`.
- **hosted** needs `RH_OFFLINE_TOKEN`, or `RH_CLIENT_ID`/`RH_CLIENT_SECRET`
  for a service account, plus a `distro`. The default upload is `aws.s3`,
  which gives a download URL.

The hosted API has its own JSON format. bpkit converts your blueprint to it
and stops with a list of anything the service can't express (user shells,
`services.default_target`, …). `--lossy` drops those settings instead.

The same operations are available from Python: `bpkit.build.build()`,
`bpkit.composer.ComposerClient`, `bpkit.hosted.HostedClient`.

## 11. Auditing

```
bpkit audit fleet.py                       # every concrete class, offline checks
bpkit audit fleet.py:WebServer --strict    # fail on warnings too (CI)
bpkit audit fleet.py --advisories --depsolve composer
bpkit audit fleet.py:WebServer --advisories --packages rpms.txt
```

```
staging-web:
  warning  BP010 users dev: no SSH key or password; this user can't log in  (StagingWeb)
  info     BP092 files /etc/motd.d/10-banner: overlay in WebServer replaces the one from Base  (WebServer)
  -- 0 error(s), 1 warning(s), 1 info
```

The offline checks cover:

- values image-builder would reject: hostnames, timezones, firewall ports,
  mountpoints, file paths
- settings that contradict each other, e.g. SSH keys with `sshd` masked
- risky settings: plain-text passwords, world-writable modes, secrets in
  files, HTTP repositories, `selinux=0`
- overlay usage: overlays that contribute nothing, and files that are
  shadowed or replaced

Each finding names the class (and overlay) that set the value. Exit status is
1 on errors, or on warnings too with `--strict`. `--json` gives
machine-readable output.

Silence a code with `--ignore BP046`, or for a class and its subclasses:

```python
class Lab(WebServer):
    AUDIT_IGNORE = ["BP011"]      # plain-text password is fine in the lab
```

For advisories, bpkit needs exact package versions. Get them from an
osbuild-composer depsolve (`--depsolve composer`) or from
`rpm -qa --qf '%{NAME} %{EPOCHNUM}:%{VERSION}-%{RELEASE} %{SOURCERPM}\n'`
output (`--packages FILE`). They are checked against OSV.dev, which covers
RHEL, AlmaLinux and Rocky Linux. `bpkit.audit` lists every code.

## Gotchas

- **Unknown fields:** a lowercase class attribute that isn't a blueprint field
  is an error (`pakages` → "did you mean 'packages'?"). Name your own
  attributes `UPPER_CASE` or `_private`.
- **Duplicate keys:** listing the same key twice in one declaration is an
  error. Across classes, the child's entry replaces the parent's.
- **Removing scalars:** `None` is rejected for scalars. Use `Unset`.
- **Bulk render:** `bpkit render module` only renders classes *defined* in
  that module, not ones it imports, and skips `abstract=True` classes.
