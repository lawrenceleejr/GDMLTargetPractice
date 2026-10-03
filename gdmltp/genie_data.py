"""Fetch the GENIE data a HEDIS (high-energy DIS) run needs, verify it, and write
the environment that points the GDMLTP GENIE image at it.

    python -m gdmltp.genie_data install      # find on /cvmfs or download; verify; write env files
    python3 gdmltp/genie_data.py install     # same, without importing the gdmltp package
    python -m gdmltp.genie_data status       # what is installed, and where it came from
    python -m gdmltp.genie_data check DIR    # compare an existing copy against the pinned files

Everything lands in one data directory (default ~/.local/share/gdmltp, or
$GDMLTP_DATA_DIR), under genie/<bundle>/. Two env files are written next to it:

  genie/genie-hedis.env  KEY=VALUE, for `docker run --env-file` (paths assume the
                         data directory is mounted at /data/gdmltp; see --mount-point)
  genie/genie-hedis.sh   `source` it inside a container or OSG job; paths resolve
                         relative to the script itself, so any mount point works

Data found on CVMFS is used in place (never copied) -- but only if it matches the
pinned files, so a different version on CVMFS can't slip in. Downloads go to a
temporary name, are checked against the pinned SHA-256, and only then move into
place, so an interrupted download never leaves a half-written file behind.
"""
import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    from gdmltp.genie_data_manifest import BUNDLES
except ImportError:
    # Run as a plain script (python3 gdmltp/genie_data.py ...), e.g. on an OSG
    # access point without the toolkit's dependencies: importing the gdmltp
    # package pulls in numpy and friends, but this tool only needs the stdlib
    # and the manifest next to it -- and Python puts this file's own directory
    # first on sys.path when it is run as a script.
    from genie_data_manifest import BUNDLES

DEFAULT_BUNDLE = "hedis-GHE19_00a"
DEFAULT_MOUNT = "/data/gdmltp"
ENV_NAME = "genie-hedis"
MARKER = ".gdmltp-complete.json"
STATE = "state.json"
RUNTIME = "runtime.json"   # read by genie/run_genie.py inside the container
DEFAULT_IMAGE = "ghcr.io/lawrenceleejr/gdmltargetpractice-genie:main"
SMALL = 64 * 1024        # files this small are always hashed, even on CVMFS spot checks


class DataError(RuntimeError):
    """A problem the user can act on; printed without a traceback."""


def default_dest():
    if os.environ.get("GDMLTP_DATA_DIR"):
        return Path(os.environ["GDMLTP_DATA_DIR"]).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share")
    return Path(base) / "gdmltp"


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# --- checking a directory against pinned files ---------------------------------

def check_tree(root, files, full=False):
    """Problems found comparing `root` against the pinned (path, size, sha256)
    list; empty if it matches. Sizes are always checked (cheap, even on CVMFS);
    contents are hashed for small files, or for everything when `full`."""
    root = Path(root)
    problems = []
    for rel, size, sha in files:
        p = root / rel
        try:
            st = p.stat()
        except OSError:
            problems.append(f"missing {rel}")
            continue
        if st.st_size != size:
            problems.append(f"size differs: {rel}")
        elif (full or size <= SMALL) and sha256_file(p) != sha:
            problems.append(f"content differs: {rel}")
    return problems


def find_on_cvmfs(comp, extra_dirs, full, log):
    """First CVMFS directory holding exactly the pinned files, or None.
    Checking the full path matters: autofs mounts a repository only when a
    path inside it is accessed (a bare `ls /cvmfs` won't show it)."""
    for d in list(extra_dirs) + list(comp.get("cvmfs_dirs", [])):
        try:
            if not Path(d).is_dir():
                continue
        except OSError:
            continue
        problems = check_tree(d, comp["files"], full=full)
        if not problems:
            return Path(d)
        log(f"  {d} exists but does not match the pinned data "
            f"({problems[0]}; {len(problems)} problem(s)) -- not using it")
    return None


# --- downloading ---------------------------------------------------------------

def _place(part, out, url, digest, n, sha256, size):
    """Verify a finished download and move it into place."""
    if size is not None and n != size:
        raise DataError(f"{url}: got {n} bytes, expected {size}")
    if sha256 is not None and digest != sha256:
        raise DataError(f"{url}: checksum mismatch (got {digest})")
    os.replace(part, out)
    return digest


def _download_cli(url, part, log=None):
    """Download with the system curl (or wget). Some servers -- SciSoft among
    them -- only accept requests from these clients and refuse Python's HTTP
    library with a 403, whatever it calls itself."""
    tools = (("curl", ["curl", "-fsSL", "--retry", "2", "-o", str(part), url]),
             ("wget", ["wget", "-q", "--tries=2", "-O", str(part), url]))
    for tool, cmd in tools:
        if shutil.which(tool):
            if log:
                log(f"  server refused Python's downloader (HTTP 403); retrying with {tool}")
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode == 0:
                return
            part.unlink(missing_ok=True)
            raise DataError(f"{url}: {tool} failed too (exit {r.returncode}) "
                            f"{r.stderr.strip()[:200]}")
    raise DataError(f"{url}: HTTP 403, and neither curl nor wget is installed to retry "
                    f"with. Install one, or download the file yourself and pass it in.")


def download(url, out, sha256=None, size=None, retries=3, log=None):
    """Download `url` to `out` atomically: write `out`.part, verify, rename.
    Returns the SHA-256 of what was downloaded. A 403 is retried once through
    curl/wget (see _download_cli); other 4xx errors are not retried."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + ".part")
    last = None
    for attempt in range(1, retries + 1):
        try:
            h = hashlib.sha256()
            n = 0
            req = urllib.request.Request(url, headers={"User-Agent": "gdmltp-genie-data"})
            with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
                for block in iter(lambda: r.read(1 << 20), b""):
                    f.write(block)
                    h.update(block)
                    n += len(block)
            return _place(part, out, url, h.hexdigest(), n, sha256, size)
        except urllib.error.HTTPError as e:
            part.unlink(missing_ok=True)
            if e.code == 403:
                _download_cli(url, part, log)
                try:
                    return _place(part, out, url, sha256_file(part), part.stat().st_size,
                                  sha256, size)
                finally:
                    part.unlink(missing_ok=True)
            if 400 <= e.code < 500 and e.code not in (408, 429):
                raise DataError(f"{url}: HTTP {e.code} {e.reason}. The server refused the "
                                f"request, so it was not retried; check the URL.") from None
            last = e
        except (OSError, DataError) as e:
            part.unlink(missing_ok=True)
            last = e
        if attempt < retries:
            if log:
                log(f"  retrying {url} ({last})")
            time.sleep(2 * attempt)
    raise DataError(f"download failed after {retries} attempts: {last}")


def fetch_files(comp, target, jobs, log):
    """Make `target` hold exactly the pinned files, downloading what is missing
    or wrong. Files already present and verified are kept."""
    todo = [(rel, size, sha) for rel, size, sha in comp["files"]
            if not ((target / rel).is_file()
                    and (target / rel).stat().st_size == size
                    and sha256_file(target / rel) == sha)]
    total = sum(s for _, s, _ in todo)
    if not todo:
        return
    log(f"  downloading {len(todo)} file(s), {total / 1e6:.0f} MB, from {comp['base_url']}")
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futs = {pool.submit(download, comp["base_url"] + urllib.parse.quote(rel),
                            target / rel, sha, size, log=log): rel
                for rel, size, sha in todo}
        for fut in as_completed(futs):
            fut.result()          # re-raises the first failure
            done += 1
            if done % 25 == 0 or done == len(todo):
                log(f"  [{done}/{len(todo)}]")


# --- the spline tarball --------------------------------------------------------

def _extract_member_dir(tarball, member_dir, dest):
    """Copy the files directly inside `member_dir` (matched as a path suffix,
    e.g. 'GHE1900a00000-k55-e5000/data') from `tarball` into `dest`.

    Regular files are written by name only, so nothing in the archive can place
    a file outside `dest`. Symlinks are kept only when they point at another
    file in the same directory (genie_xsec ships gxspl-FNALsmall.xml as an alias
    of gxspl-NUsmall.xml); any other link is skipped."""
    want = member_dir.strip("/")
    n, links = 0, []
    with tarfile.open(tarball, "r:*") as tf:
        for m in tf:
            parent, _, name = m.name.rstrip("/").rpartition("/")
            if not (parent == want or parent.endswith("/" + want)):
                continue
            if m.issym():
                links.append((name, m.linkname))
            elif m.isfile():
                src = tf.extractfile(m)
                with open(dest / name, "wb") as out:
                    shutil.copyfileobj(src, out)
                n += 1
    for name, target in links:
        if "/" in target or target in ("", ".", "..") or not (dest / target).is_file():
            continue
        os.symlink(target, dest / name)
        n += 1
    if n == 0:
        raise DataError(f"{tarball}: nothing found under '{want}/' -- wrong tarball?")
    return n


def install_tarball(comp, target, local_tarball, allow_unverified, log):
    """Install the tarball's data directory into `target`. Returns the tarball's
    SHA-256 (the component's fingerprint) and the SHA-256 of each installed
    regular file, so a later --verify can check them without the tarball."""
    staging = target.with_name(target.name + ".staging")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        want = comp.get("sha256")
        if local_tarball:
            tarball = Path(local_tarball).expanduser()
            log(f"  using local tarball {tarball}")
            digest = sha256_file(tarball)
        else:
            tarball = staging / Path(urllib.parse.urlparse(comp["url"]).path).name
            if want is None and not allow_unverified:
                raise DataError(
                    "the spline tarball has no pinned sha256 in the manifest, so a "
                    "download can't be verified.\n  Set 'sha256' for the splines in "
                    "gdmltp/genie_data_manifest.py (run sha256sum on the tarball you "
                    "know is good), or pass --allow-unverified.")
            log(f"  downloading {comp['url']}")
            digest = download(comp["url"], tarball, sha256=want, log=log)
        if want is not None and digest != want:
            raise DataError(f"{tarball}: sha256 {digest} does not match the manifest "
                            f"({want}). Is this the right version of the splines?")
        if want is None:
            log(f"  WARNING: unverified spline tarball, sha256 {digest}\n"
                f"           put this in the manifest once you have confirmed it is right")
        files = staging / "files"
        files.mkdir()
        n = _extract_member_dir(tarball, comp["member_dir"], files)
        log(f"  extracted {n} file(s)")
        hashes = {p.name: sha256_file(p) for p in sorted(files.iterdir())
                  if p.is_file() and not p.is_symlink()}
        shutil.rmtree(target, ignore_errors=True)
        os.replace(files, target)
        return digest, hashes
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def pick_xsec_file(target, name):
    """The spline file GENIE_XSEC_FILE will name. It must be one of the
    installed files."""
    if not (target / name).is_file():
        have = sorted(p.name for p in target.iterdir()
                      if not p.name.startswith(".")) if target.is_dir() else []
        raise DataError(f"{name} is not among the installed spline files "
                        f"({', '.join(have) or 'none'}). Choose one with --xsec-file.")
    return name


def tarball_intact(target, marker):
    """True if every file recorded at install time is present and unchanged."""
    files = marker.get("files") or {}
    return bool(files) and all(
        (target / n).is_file() and sha256_file(target / n) == sha for n, sha in files.items())


# --- env files -----------------------------------------------------------------

def write_runtime(root, bundle_name, bundle, resolved):
    """Describe the installed bundle for the GENIE driver in the container:
    which tune it serves and what each variable should be. CVMFS locations are
    absolute; local data is relative to the data directory's genie/ folder, so
    the description holds wherever that directory is mounted. The driver
    applies it only to runs with this tune."""
    env = {var: ({"cvmfs": path} if where == "cvmfs" else {"rel": path})
           for var, (where, path) in sorted(resolved.items())}
    tmp = root / (RUNTIME + ".tmp")
    tmp.write_text(json.dumps({"bundle": bundle_name, "tune": bundle["tune"],
                               "genie_version": bundle["genie_version"],
                               "env": env}, indent=2) + "\n")
    os.replace(tmp, root / RUNTIME)


def write_env_files(genie_dir, resolved, mount_point):
    """`resolved` maps VAR -> absolute CVMFS path, or a path relative to
    genie_dir for data installed locally."""
    env_lines = ["# Generated by `python -m gdmltp.genie_data install` -- do not edit.",
                 f"# Local paths assume the data directory is mounted at {mount_point}."]
    sh_lines = ["# Generated by `python -m gdmltp.genie_data install` -- do not edit.",
                "# Source inside a container or OSG job to point GENIE at the data.",
                '_gdmltp_here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"']
    for var, (where, path) in sorted(resolved.items()):
        if where == "cvmfs":
            env_lines.append(f"{var}={path}")
            sh_lines.append(f'export {var}="{path}"')
        else:
            env_lines.append(f"{var}={mount_point.rstrip('/')}/genie/{path}")
            sh_lines.append(f'export {var}="${{_gdmltp_here}}/{path}"')
    sh_lines.append("unset _gdmltp_here")
    (genie_dir / f"{ENV_NAME}.env").write_text("\n".join(env_lines) + "\n")
    (genie_dir / f"{ENV_NAME}.sh").write_text("\n".join(sh_lines) + "\n")


# --- commands ------------------------------------------------------------------

def _read_marker(target):
    try:
        return json.loads((target / MARKER).read_text())
    except (OSError, ValueError):
        return {}


def _marker_ok(target, fingerprint):
    return _read_marker(target).get("fingerprint") == fingerprint


def _write_marker(target, fingerprint, **extra):
    (target / MARKER).write_text(json.dumps(dict(fingerprint=fingerprint,
                                                 installed=_now(), **extra)) + "\n")


def cmd_install(args, log=print):
    bundle = BUNDLES[args.bundle]
    dest = Path(args.dest).expanduser().resolve()
    genie_dir = dest / "genie"
    root = genie_dir / args.bundle
    root.mkdir(parents=True, exist_ok=True)
    resolved, sources = {}, {}
    log(f"Installing {args.bundle}: {bundle['description']}")
    for name, comp in bundle["components"].items():
        target = root / name
        log(f"[{name}]")
        if comp["kind"] == "files":
            found = None if args.prefer == "download" else find_on_cvmfs(
                comp, args.cvmfs_dir, args.verify, log)
            if found is not None:
                log(f"  using CVMFS copy {found} (matches pinned commit {comp['commit'][:7]})")
                base, where = found, "cvmfs"
            else:
                if args.prefer == "cvmfs":
                    raise DataError("no matching CVMFS copy found and --prefer cvmfs given")
                if args.verify or not _marker_ok(target, comp["commit"]):
                    target.mkdir(parents=True, exist_ok=True)
                    fetch_files(comp, target, args.jobs, log)
                    _write_marker(target, comp["commit"])
                else:
                    log("  already installed")
                base, where = Path(args.bundle) / name, "local"
            sources[name] = {"source": where, "path": str(found or target),
                             "commit": comp["commit"]}
            for var, sub in comp["env"].items():
                resolved[var] = (where, str(base / sub) if where == "cvmfs"
                                 else (base / sub).as_posix())
        elif comp["kind"] == "tarball":
            pinned, marker = comp.get("sha256"), _read_marker(target)
            fingerprint = marker.get("fingerprint")
            current = fingerprint is not None and pinned in (None, fingerprint)
            if args.spline_tarball or not current:
                fingerprint, hashes = install_tarball(comp, target, args.spline_tarball,
                                                      args.allow_unverified, log)
                _write_marker(target, fingerprint, files=hashes)
            elif args.verify and not tarball_intact(target, marker):
                log("  installed spline files differ from what was installed; reinstalling")
                fingerprint, hashes = install_tarball(comp, target, None,
                                                      args.allow_unverified, log)
                _write_marker(target, fingerprint, files=hashes)
            else:
                log("  verified" if args.verify else "  already installed")
            xsec = pick_xsec_file(target, args.xsec_file or comp["default_xsec_file"])
            log(f"  GENIE_XSEC_FILE -> {xsec}")
            sources[name] = {"source": "local", "path": str(target),
                             "sha256": fingerprint, "xsec_file": xsec}
            resolved["GENIE_XSEC_FILE"] = ("local", f"{args.bundle}/{name}/{xsec}")
        else:
            raise DataError(f"unknown component kind {comp['kind']!r}")
    write_env_files(genie_dir, resolved, args.mount_point)
    write_runtime(root, args.bundle, bundle, resolved)
    (root / STATE).write_text(json.dumps({"bundle": args.bundle, "installed": _now(),
                                          "genie_version": bundle["genie_version"],
                                          "tune": bundle["tune"],
                                          "components": sources}, indent=2) + "\n")
    _print_usage(dest, genie_dir, args.mount_point, sources, log)
    return 0


def _print_usage(dest, genie_dir, mount, sources, log):
    env_file, sh_file = genie_dir / f"{ENV_NAME}.env", genie_dir / f"{ENV_NAME}.sh"
    log("\nDone. To use it, set GENIE once (e.g. in your GDMLrc.sh) so it also mounts the data:")
    log(f'    export GENIE="-v {dest}:{DEFAULT_MOUNT}:ro {DEFAULT_IMAGE}"')
    log("  then run as usual:")
    log("    gtp $GENIE run --config <cfg>.yaml")
    log("  Runs with this bundle's tune pick the data up automatically; other tunes ignore it.")
    log("  Leave `cross_sections` unset (or 'auto') in your YAML so the installed splines are used.")
    log(f"  Without that mount (bare docker, OSG jobs): pass --env-file {env_file},")
    log(f"  or `source <data dir>/genie/{sh_file.name}` inside the container.")
    if any(s["source"] == "cvmfs" for s in sources.values()):
        log("  Some data is used from CVMFS: jobs must run where that repository is\n"
            "  mounted (on OSG: HAS_CVMFS_fermilab_opensciencegrid_org == True).")


def cmd_status(args, log=print):
    dest = Path(args.dest).expanduser()
    root = dest / "genie" / args.bundle
    try:
        state = json.loads((root / STATE).read_text())
    except (OSError, ValueError):
        log(f"{args.bundle}: not installed in {dest} (run: python -m gdmltp.genie_data install)")
        return 1
    log(f"{args.bundle} (GENIE {state['genie_version']}, tune {state['tune']}), "
        f"installed {state['installed']}")
    ok = True
    for name, info in state["components"].items():
        present = Path(info["path"]).is_dir()
        ok &= present
        log(f"  {name:8s} {info['source']:5s} {info['path']}"
            + (f"  (GENIE_XSEC_FILE: {info['xsec_file']})" if "xsec_file" in info else "")
            + ("" if present else "   <-- NOT FOUND"))
    return 0 if ok else 1


def cmd_check(args, log=print):
    comp = BUNDLES[args.bundle]["components"]["he_data"]
    problems = check_tree(args.dir, comp["files"], full=not args.quick)
    if problems:
        for p in problems[:20]:
            log(f"  {p}")
        log(f"{args.dir}: {len(problems)} problem(s) versus pinned commit {comp['commit'][:7]}")
        return 1
    log(f"{args.dir}: matches pinned commit {comp['commit'][:7]} "
        f"({len(comp['files'])} files{'' if args.quick else ', all contents verified'})")
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="python -m gdmltp.genie_data",
                                description="Fetch and verify GENIE data for HEDIS runs.")
    sub = p.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dest", default=str(default_dest()),
                        help="data directory (default: %(default)s; or set GDMLTP_DATA_DIR)")
    common.add_argument("--bundle", default=DEFAULT_BUNDLE, choices=sorted(BUNDLES))

    i = sub.add_parser("install", parents=[common], help="find or download, verify, write env files")
    i.add_argument("--prefer", choices=["auto", "cvmfs", "download"], default="auto",
                   help="auto: use a matching CVMFS copy if present, else download")
    i.add_argument("--cvmfs-dir", action="append", default=[], metavar="DIR",
                   help="extra directory to check before the standard CVMFS location")
    i.add_argument("--spline-tarball", metavar="FILE",
                   help="install splines from this local tarball instead of downloading")
    i.add_argument("--xsec-file", metavar="NAME",
                   help="spline file (from the installed set) that GENIE_XSEC_FILE names; "
                        "default: the bundle's default_xsec_file")
    i.add_argument("--allow-unverified", action="store_true",
                   help="accept a spline tarball with no pinned sha256 (prints its hash)")
    i.add_argument("--verify", action="store_true",
                   help="re-verify every file, including full hashes of CVMFS copies")
    i.add_argument("--mount-point", default=DEFAULT_MOUNT,
                   help="where the data dir appears inside the container (default: %(default)s)")
    i.add_argument("--jobs", type=int, default=8, help="parallel downloads (default: 8)")
    i.set_defaults(func=cmd_install)

    s = sub.add_parser("status", parents=[common], help="show what is installed")
    s.set_defaults(func=cmd_status)

    c = sub.add_parser("check", parents=[common],
                       help="compare an existing HEDIS data copy against the pinned files")
    c.add_argument("dir", help="directory containing hedis-sf/, photon-sf/, pdfs/")
    c.add_argument("--quick", action="store_true", help="sizes only (plus small files)")
    c.set_defaults(func=cmd_check)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except DataError as e:
        print(f"gdmltp genie-data: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())