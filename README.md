# bpkit

Define [osbuild / image-builder](https://osbuild.org) **blueprints as Python
classes** and render them to blueprint TOML.

Blueprints have no include/extends mechanism, so teams copy-paste a base
blueprint across variants. With bpkit a base class holds the common config,
mixins add concerns (hardening, monitoring), and subclasses add or override,
just like normal Python inheritance. The output is plain blueprint TOML for
`image-builder build` / `composer-cli`, so bpkit only depends on the stable
blueprint format.

```python
from bpkit import Blueprint, Services, User, Filesystem, Overlay, Remove, Replace


class Base(Blueprint, abstract=True):
    """Common base for all our images."""

    packages = ["vim", "tmux"]
    services = Services(enabled=["sshd"])
    timezone = "Australia/Sydney"
    users = [User(name="ops", groups=["wheel"], key="ssh-ed25519 AAAA...")]
    overlay = Overlay("overlays/base")  # config files from a directory tree


class Hardened(Blueprint, abstract=True):  # mixin
    services = Services(enabled=["firewalld"], masked=["cups"])
    kernel_append = ["audit=1"]


class WebServer(Base, Hardened):
    packages = ["nginx"]  # ADDS → vim, tmux, nginx
    timezone = "UTC"  # scalar → OVERRIDES
    filesystem = [Filesystem("/var", "10 GiB")]


class StagingWeb(WebServer):
    packages = ["htop", Remove("tmux")]  # add htop, drop inherited tmux


class Minimal(WebServer):
    packages = Replace(["nginx"])  # discard everything inherited


bp = StagingWeb(hostname="staging-web01")  # instance kwargs override too
print(bp.to_toml())
bp.write("staging-web.toml")
```

See [`examples/fleet.py`](examples/fleet.py) for a fuller example and
[`tests/golden/`](tests/golden) for what it renders to.

## Install

```bash
pip install -e ".[dev]"     # Python ≥ 3.11; deps: pydantic>=2.11, tomli-w
pytest && ruff check .
```

## How resolution works

Each class is resolved when it is defined. bpkit walks `reversed(cls.__mro__)`
and, for every `Blueprint` subclass, folds in **only the attributes declared in
that class's own body**. Mixins and diamond hierarchies therefore behave like
normal Python: each class applies once, and for scalars the leftmost base wins,
just as with attribute lookup. Errors (a bad `Remove`, an invalid path, a
misspelt field) are raised on import, not at render time.

Instances get a deep copy of the resolved values, so mutating one never leaks
into the class or its siblings.

### Merge rules

| Field kind | Fields | Rule |
|---|---|---|
| Scalars | `distro`, `architecture`, `hostname`, `timezone`, `fips`, `partitioning_mode`, `installation_device` | Child overrides |
| Keyed lists | `packages`, `modules`, `groups`, `enabled_modules` (by `name`); `users`, `user_groups` (by `name`); `sshkeys` (by `user`); `filesystem` (by `mountpoint`); `files`, `directories` (by `path`); `repositories` (by `id`); `containers` (by `source`) | Union by key; a child entry replaces the parent entry with the same key in place; first-seen order |
| String sets | `kernel_append`, `ntpservers` | Ordered set union |
| Structs | `services`, `firewall`, `locale`, `kernel`, `installer` | Field by field: string lists union, scalars override, nested structs recurse |
| Passthrough | `extra_customizations` | Deep merge (dicts recurse, everything else replaces) |
| Identity | `name`, `description`, `version` | **Not inherited.** `name` defaults to the kebab-cased class name (`StagingWeb` → `staging-web`), `description` to the first line of the class's own docstring, `version` to `"0.0.1"` |

`kernel_append` is a list in Python and a space-joined string in TOML
(`customizations.kernel.append`). `timezone` and `ntpservers` both render into
`customizations.timezone`.

**Service conflicts:** if a child enables a service that a parent disabled or
masked (or the other way round), the child's intent wins and the service is
moved. It is never listed twice. The same applies to `firewall.services` and
`installer.modules`. Listing a service in two lists in one declaration is an
error.

### Markers

| Marker | Example | Effect |
|---|---|---|
| `Remove(key, ...)` | `packages = ["htop", Remove("tmux")]` | Drops inherited entries by key. Removing a key that isn't inherited **raises**, which catches typos. Also works in string sets and inside structs (`Services(masked=[Remove("cups")])`). |
| `Replace(value)` | `packages = Replace(["nginx"])` | Discards everything inherited for that field (or struct sub-field: `Services(enabled=Replace([...]))`). |
| `Unset` | `timezone = Unset` | Removes an inherited value so it is omitted from the output. |

Markers are always resolved away; none reach `to_dict()`. An abstract mixin
may `Remove` something it doesn't inherit itself. The check runs when a
concrete class pulls the mixin in.

### Shorthands

- `"vim"` → `Package("vim", "*")`; `Package("vim", "9.1*")`
- `"nodejs:20"` in `enabled_modules` → `EnabledModule("nodejs", "20")`
- `"Development Tools"` in `groups`; `"quay.io/x:1"` in `containers`
- `Filesystem("/var", "10 GiB")`: size is a string with units (`kB`, `KiB`, `MB`, `MiB`, `GB`, `GiB`, `TB`, `TiB`, the same as image-builder) or int bytes; the mountpoint must be absolute
- `File(path, data, user, group, mode)`: mode as `"0644"` or `0o644`
- Plain dicts are accepted anywhere a model is

## Overlay import

Point a blueprint at a directory laid out like the target filesystem, and
bpkit turns it into `files` and `directories` customizations:

```
overlays/web/
  etc/nginx/conf.d/site.conf
  etc/motd.d/10-banner
  usr/local/bin/healthcheck        # 0755 on disk → mode = "0755"
  var/www/html/static/.gitkeep     # marks an empty directory
```

```python
class WebServer(Base):
    overlay = Overlay("overlays/web")  # relative to this .py file
    # or: overlay = ["overlays/web", Overlay("nginx", prefix="/etc/nginx")]
```

- Every regular file becomes a `File` with inline UTF-8 contents. Its mode is
  taken from disk unless you pass `file_mode=` or `preserve_mode=False`.
- Every leaf directory becomes a `Directory` with `ensure_parents = true`, so
  missing parents are created. No mode, user or group is set unless you pass
  `dir_mode=` / `user=` / `group=`. image-builder then creates the directory
  with `exist_ok`, so directories the distro already ships (`/etc/nginx`) are
  fine. Directories that always exist (`/etc`, `/usr/local/bin`, …) are skipped.
  Use `directories="all"` or `"none"` to change this.
- Overlay entries merge by path like any other entry. A child's overlay file
  replaces the parent's (the overlayfs mental model), explicit `files =
  [...]` in the same class win over its overlay, and
  `files = [Remove("/etc/motd")]` drops one.
- Drop an inherited overlay like removing a layer from an overlayfs stack:
  whatever it was covering (a lower overlay's file, or an explicit `File`)
  comes back.

  ```python
  class NoDebug(WebDebug):
      overlay = [Remove("overlays/debug")]          # just that one
  class Bare(WebDebug):
      overlay = Unset                               # all inherited overlays
  class Swap(WebDebug):
      overlay = Replace(Overlay("overlays/other"))  # drop all, add this
  ```

  `Remove` takes the overlay's path. It is resolved against the removing
  class's module first, then matched as a unique trailing path
  (`"overlays/debug"`). Removing an overlay that isn't inherited raises.
- The overlay is read when the class is defined, and these problems are
  reported then: symlinks, non-UTF-8 files, and paths image-builder's default
  policy refuses (`/usr` outside `/usr/local`, `/boot`, `/etc/passwd`, …).
  `check_policy=False` turns the path check off, for example for ostree image
  types with different rules.
- Default excludes: `.gitkeep`, `.keep`, `.git`, `.DS_Store`, `*.swp`, `*~`,
  `*.orig`, `*.rej`. Pass `exclude=[...]` to change them.

`bpkit overlay DIR [--prefix /etc/app]` prints the generated TOML without a
blueprint class.

## Hooks and overrides

```python
class Monitored(Blueprint, abstract=True):
    def customize(self) -> None:
        super().customize()  # cooperative chaining
        self.packages.add("node-exporter")
        self.services.enable("node-exporter")  # moves it out of disabled/masked


class App(Monitored, WebServer):
    def customize(self) -> None:
        super().customize()
        if os.environ.get("DEBUG"):
            self.packages.add("gdb")
        self.packages.remove("tmux")  # KeyError if missing
        self.hostname = f"app-{self.version}"  # assignment replaces the value
```

Constructor kwargs follow the same merge rules as a subclass:
`WebServer(hostname="web01", packages=["curl"])`. Then `customize()` runs on
the instance.

Helpers: keyed lists (`self.packages`, `self.users`, `self.files`, …) have
`add`, `remove`, `discard`, `get`, `in` and lookup by key (`self.users["ops"]`).
String sets have `add`, `remove` and `discard`. `Services` has
`enable`/`disable`/`mask`/`reset`, `Firewall` has `open_port`/`close_port`,
and `FirewallServices` has `enable`/`disable`.

## Output

- `to_dict()`: a plain dict in blueprint shape, using TOML names
  (`tls_verify` → `tls-verify`, `users` → `customizations.user`,
  `user_groups` → `customizations.group`). Empty and unset fields are omitted.
- `to_toml()` (via `tomli-w`), `write(path)`. Key and list order are stable,
  so diffs in git stay clean.
- `explain(field=None)` shows which class contributed each value. It works on
  a class or an instance, and instances also report `<kwargs>` and
  `customize()`:

  ```
  $ bpkit explain examples/fleet.py:StagingWeb packages
  packages:
    audit         <- Hardened
    vim-enhanced  <- Base
    tmux          <- Base
    chrony        <- Base
    nginx         <- WebServer
    htop          <- StagingWeb
    strace        <- StagingWeb
  ```

  (`aide` is missing because `StagingWeb` declares `Remove("aide")`.)

- `extra_customizations = {"openscap": {...}}` passes through anything not
  modelled yet (openscap, ignition, fdo, disk, …). It is deep-merged into
  `[customizations]`.

## CLI

Targets are `module.path:Class` or `path/to/file.py:Class`.

```bash
bpkit render examples/fleet.py:StagingWeb [-o staging.toml]   # stdout by default
bpkit render examples/fleet.py -o out/        # every concrete class → out/<name>.toml
bpkit list examples/fleet.py                  # classes, bases, abstract or not
bpkit diff examples/fleet.py:WebServer examples/fleet.py:MinimalWeb   # exit 1 if different
bpkit explain examples/fleet.py:StagingWeb [field]
bpkit overlay overlays/web [--prefix /] [--directories leaves|all|none]
```

Mark bases and mixins `abstract=True` so bulk render skips them. Bulk render
only picks up classes **defined** in the module, not ones it imports.

## Design notes

- Field names and types were checked against `osbuild/blueprint`
  (`pkg/blueprint`, which `osbuild/images` now imports). The path policies
  for overlays come from `osbuild/images` `pkg/policies`.
- Lowercase public class attributes that aren't blueprint fields are an error
  (`pakages = [...]` → "did you mean 'packages'?"). Use `UPPER_CASE` or
  `_private` names for your own attributes.
- `name`, `description` and `version` are per class, not inherited.

## Docs

The usage guide is [`docs/guide.md`](docs/guide.md). pdoc renders it as the
landing page, with the API reference after it:

```bash
pdoc bpkit                     # live preview in the browser
pdoc -o docs/html bpkit        # static HTML
```

Published docs: <https://drcoffee.github.io/osbuilder-py-test/>. The
[Docs workflow](.github/workflows/docs.yml) rebuilds and deploys them on every
push to `main`. Pull requests build the site and run the guide's examples
without deploying.

`tests/test_docs.py` runs every Python block in the guide and checks the
`explain()` sample against real output, so the guide can't drift from the code.

## Tests

`pytest` runs unit, round-trip (`tomllib`), golden-file and CLI tests.
Regenerate the golden files with `BPKIT_UPDATE_GOLDEN=1 pytest tests/test_golden.py`.
If `image-builder` is on `PATH`, an integration test also runs
`image-builder manifest qcow2 --blueprint …` on each example; set
`BPKIT_TEST_DISTRO` to choose the distro (default `fedora-42`).

## Auditing

```bash
bpkit audit fleet.py [--strict] [--json] [--ignore BP046] [--hosted]
bpkit audit fleet.py --advisories --depsolve composer      # or --packages rpm-qa.txt
```

The offline checks flag:

- values image-builder would reject
- contradictory settings (SSH keys with `sshd` masked, firewall rules with
  `firewalld` off)
- risky settings: plain-text passwords, world-writable or setuid modes,
  secrets in files, HTTP or unsigned repositories, kernel args like
  `selinux=0`, legacy daemons
- overlay usage: empty overlays, shadowed or replaced files

`--advisories` looks up known advisories for exact package versions on
OSV.dev (RHEL, AlmaLinux and Rocky Linux). Every finding has a stable code
(the table is in the `bpkit.audit` docs), names the class and overlay that
set the value, and can be silenced per class with
`AUDIT_IGNORE = ["BP011"]`.

In CI, [`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs ruff, the
tests on Python 3.11 to 3.13, and `bpkit audit` on `AUDIT_TARGETS` for every
push to `main` and every pull request. The audit report goes to the job
summary and a JSON artifact. Set `AUDIT_ARGS: --strict` to fail on warnings
too.

## Importing and building

```bash
bpkit import web.toml db.toml --base Fleet -o fleet.py [--overlay-dir overlays]
bpkit import --from-composer web-server -o web.py

bpkit build fleet.py:WebServer -t qcow2                          # local image-builder
bpkit build fleet.py:WebServer -t qcow2 --backend composer       # osbuild-composer (Weldr socket)
bpkit build fleet.py:WebServer -t guest-image --backend hosted   # console.redhat.com
bpkit push fleet.py:WebServer [--backend hosted --type guest-image]
bpkit status <id> [--backend hosted]
```

- **Import:** you get one class per blueprint, and `--base` factors the
  shared settings into an abstract class. The output renders back to the
  same blueprints; only list order can change.
- **Hosted credentials:** `RH_OFFLINE_TOKEN`, or `RH_CLIENT_ID` and
  `RH_CLIENT_SECRET` for a service account.
- **Hosted conversion:** blueprints are converted to the service's JSON
  format. Settings it can't express are reported as errors unless you pass
  `--lossy`.
- **No extra dependencies:** the clients only use the standard library.
  They are tested against fake servers, not the live services.
