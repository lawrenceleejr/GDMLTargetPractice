#!/usr/bin/env python3
"""In-container GENIE driver for GDMLTargetPractice.

Reads a `genie_job.json` (written on the host by gdmltp's genie backend) and runs
the generation pipeline, ending in an `output.root` in the common g4sim schema:

    gevgen  ->  gntpc -f gst  ->  genie2root (gdmltp.backends.genie_convert)

v1 scope (vertex-only): the interaction is generated on the target nucleus that
the GDML geometry selected (job["target"]), at a point vertex. Geometry-aware
vertex sampling in the full GDML volume, and handing final-state particles to
Geant4 for transport, are later phases (see the repo plan / README).

This script shells out to GENIE tools and is therefore validated by the (non-
gating) genie CI smoke test, not the pure-python unit suite.
"""
import json
import math
import os
import subprocess
import sys
from pathlib import Path

from gdmltp.backends.genie import flux_gevgen_args


def spline_emax_gev(job, beam_energies_gev=None):
    """Upper energy the cross-section splines must cover: the flux endpoint
    from the job (written by the host backend), or the hardest beam-file ray,
    with a 100 GeV floor so the common few-GeV case shares one cached file."""
    emax = float(job.get("flux_emax_gev") or 0.0)
    if beam_energies_gev:
        emax = max(emax, max(beam_energies_gev))
    return max(100.0, math.ceil(emax))


def _is_hedis(job):
    """A high-energy DIS (HEDIS) run -- GHE19 tune or a HEDIS generator list."""
    tune = str(job.get("tune", ""))
    egl = str(job.get("event_generator_list", ""))
    return tune.startswith("GHE19") or "HEDIS" in egl.upper()


_HEDIS_HELP = (
    "This is a HEDIS high-energy-DIS tune ({tune}), which needs a GENIE image "
    "built WITH HEDIS support -- APFEL and the tune's LHAPDF grid -- plus the "
    "structure-function tables that gmkhedissf builds from them. This image was "
    "not built that way (no GDMLTP_HEDIS marker), so gmkhedissf aborts.\n"
    "How to proceed:\n"
    "  * HEDIS image: rebuild the genie base with --build-arg ENABLE_HEDIS=1\n"
    "    (docker/genie-base.Dockerfile; see docs/neutrino.md 'HEDIS'). It bakes\n"
    "    in APFEL, the NNPDF31sx LHAPDF set, and the SF tables.\n"
    "  * Or use a standard tune (e.g. tune: G18_10a_00_000) for E_nu up to ~1 TeV.\n"
    "  * For a first genie smoke test, examples/nu_argon.yaml runs out of the box."
)


def _hedis_provisioned():
    """True only for an image built with HEDIS support. The base Dockerfile sets
    GDMLTP_HEDIS=1 in that case (ENABLE_HEDIS=1); default images leave it unset/0."""
    return os.environ.get("GDMLTP_HEDIS", "0") == "1"


def _ensure_hedis_sf(job, workdir):
    """HEDIS reads pre-tabulated structure-function grids (QrkSF_*.dat) from
    $HEDIS_SF_DATA_PATH, produced once by gmkhedissf --tune <tune>. Generate
    them on demand if absent (requires GENIE built with APFEL + the tune's
    LHAPDF set installed -- see docs/neutrino.md). Cached by a stamp file so it
    runs at most once per tune."""
    tune = job.get("tune", "GHE19_00a_00_000")
    sf_dir = Path(os.environ.get("HEDIS_SF_DATA_PATH",
                                 str(Path(os.environ.get("GENIE", "/opt/genie"))
                                     / "data" / "evgen" / "hedis-sf")))
    stamp = sf_dir / f".gdmltp_hedis_sf_{tune}.done"
    # gmkhedissf writes QrkSF*.dat under a per-tune subdir (<sf_dir>/<tune>/),
    # so search recursively -- baked-in tables live one level down.
    if stamp.exists() or (sf_dir.exists() and any(sf_dir.rglob("QrkSF*"))):
        return
    # Preflight: gmkhedissf aborts (SIGABRT, "Assertion `0'") on an image without
    # APFEL/LHAPDF -- turn that into a clear, actionable message up front.
    if not _hedis_provisioned():
        raise RuntimeError(_HEDIS_HELP.format(tune=tune))
    print(f"[run_genie] building HEDIS structure-function tables (gmkhedissf "
          f"--tune {tune}); slow, one-time.", flush=True)
    try:
        subprocess.run(["gmkhedissf", "--tune", tune], cwd=workdir, check=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"gmkhedissf --tune {tune} failed ({e}). The image reports HEDIS "
            f"support but the tables could not be built -- check that the tune's "
            f"LHAPDF set is installed (lhapdf ls --installed) and APFEL is on "
            f"LD_LIBRARY_PATH. See docs/neutrino.md 'HEDIS'.") from e
    try:
        sf_dir.mkdir(parents=True, exist_ok=True)
        stamp.write_text("ok\n")
    except OSError:
        pass


def _baked_hedis_spline(probe_abs, target, tune, egl, emax):
    """A HEDIS xsec spline baked into the image for this exact probe+target+
    tune+egl whose energy reach covers `emax`, or None. Filenames follow the
    driver's own cache convention (gxspl_<probe>_<target>_<tune>_<egl>_<E>gev.xml)
    so a baked file is a drop-in for what gmkspl would have produced. The energy
    is read from the filename, so a 5 TeV baked spline serves a 3 TeV request."""
    import glob
    base = os.environ.get(
        "HEDIS_XSEC_DIR",
        str(Path(os.environ.get("GENIE", "/opt/genie")) / "data" / "evgen" / "hedis-xsec"))
    if not os.path.isdir(base):
        return None
    prefix = f"gxspl_{probe_abs}_{target}_{tune}_{egl}_"
    best = None
    for p in glob.glob(os.path.join(base, prefix + "*gev.xml")):
        name = os.path.basename(p)
        try:
            e = int(name[len(prefix):-len("gev.xml")])
        except ValueError:
            continue
        if e >= emax and (best is None or e < best[0]):
            best = (e, p)          # smallest spline that still covers the range
    return best[1] if best else None


def _xsec_args(job, workdir, emax_gev=None):
    """Cross-section splines for gevgen, in priority order: an explicit path in
    the job, the image's $GENIE_XSEC_FILE, else generate them ON DEMAND with
    gmkspl into the run directory (cached there, so the mounted volume makes
    reruns fast). The on-demand path is what lets a freshly built image run with
    no manual setup at all -- the first run on a new probe/target just takes
    longer while the splines compute. `emax_gev` (see spline_emax_gev) sets the
    spline reach and is part of the cache name, so a TeV run never silently
    reuses a 100 GeV file. For HEDIS tunes the structure-function tables are
    ensured first and the generator list is passed to gmkspl (it must match
    gevgen)."""
    xsec = job.get("cross_sections", "auto")
    if xsec and xsec != "auto":
        return ["--cross-sections", xsec]
    if os.environ.get("GENIE_XSEC_FILE"):
        return ["--cross-sections", os.environ["GENIE_XSEC_FILE"]]

    probe = int(job["probe"])
    target = int(job["target"])
    tune = job.get("tune", "G18_10a_00_000")
    hedis = _is_hedis(job)
    egl = job.get("event_generator_list", "Default")
    emax = int(emax_gev if emax_gev is not None else spline_emax_gev(job))
    tag = f"{tune}_{egl}" if hedis else tune
    out = Path(workdir) / f"gxspl_{abs(probe)}_{target}_{tag}_{emax}gev.xml"
    # HEDIS xsec splines are very expensive to compute (gmkspl integrates the
    # structure functions per knot -- ~an hour even for a coarse spline). The
    # image can bake one for the shipped tune/target so the example skips that:
    # if a baked spline for this probe+target+tune+egl covers the needed energy,
    # use it directly. (HEDIS_XSEC_DIR overrides the default location.)

    # JTR: Claude wrote what is above. It is naive and incomplete. It should not
    # be allowed to write GENIE code.
    if hedis:
        baked = _baked_hedis_spline(abs(probe), target, tune, egl, emax)
        if baked is not None:
            print(f"[run_genie] using baked HEDIS spline {baked} (skips gmkspl).",
                  flush=True)
            return ["--cross-sections", str(baked)]
    if not out.exists():
        if hedis:
            _ensure_hedis_sf(job, workdir)
        print(f"[run_genie] no spline file found; generating with gmkspl for "
              f"probe {probe} on {target} (tune {tune}, up to {emax} GeV). "
              f"This is slow the first time; the result is cached in the run "
              f"directory.", flush=True)
        cmd = ["gmkspl", "-p", f"{probe},{-probe}", "-t", str(target),
               "-n", "100", "-e", str(emax), "--tune", tune]
        if hedis:
            cmd += ["--event-generator-list", egl]   # must match gevgen
        cmd += ["-o", str(out)]
        print("[run_genie]", " ".join(cmd), flush=True)
        subprocess.run(cmd, cwd=workdir, check=True)
    return ["--cross-sections", str(out)]


def run(job_path):
    job = json.loads(Path(job_path).read_text())
    workdir = Path(job_path).resolve().parent
    if job.get("beam_file"):
        return _run_beam(job, workdir)

    events = int(job["events"])
    out = job.get("output", "output.root")
    vtx_units = job.get("length_units", "cm")
    ghep = str(workdir / "genie_events.ghep.root")
    gst = str(workdir / "genie_events.gst.root")


    cmd = ["gevgen", "-n", str(events), "-p", str(job["probe"]), "-t", str(job["target"]),
           "--tune", job["tune"], "--event-generator-list", job["event_generator_list"],
           "-o", ghep]
    fargs, approx = flux_gevgen_args(job["flux"])
    if approx:
        print(f"[run_genie] WARNING: energy mode {job['flux'].get('mode')!r} is "
              f"approximated by its nominal energy in v1.", file=sys.stderr)
    cmd += fargs
    if job.get("seed") is not None:
        cmd += ["--seed", str(int(job["seed"]))]
    cmd += _xsec_args(job, workdir)

    print("[run_genie] generating:", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=workdir, check=True)
    print("[run_genie] converting GHEP -> gst ...", flush=True)
    subprocess.run(["gntpc", "-i", ghep, "-f", "gst", "-o", gst], cwd=workdir, check=True)
    print("[run_genie] converting gst -> output.root ...", flush=True)
    from gdmltp.backends import genie_convert
    genie_convert.convert(gst, str(workdir / out), vtx_units=vtx_units,
                          position=job.get("position"), direction=job.get("direction"))
    print(f"[run_genie] done -> {workdir / out}", flush=True)
    return 0


def _run_beam(job, workdir):
    """Full per-event replay of a host-sampled beam file: one gevgen call per ray
    (point mode at the ray's energy), merged, then the converter places each
    vertex and orients the event along the ray. Faithful but O(N) gevgen calls;
    a single-pass gsimple flux driver is the future scale optimization."""
    from gdmltp.beam import read_beam_file
    from gdmltp.backends import genie_convert

    entries = read_beam_file(str(workdir / job["beam_file"]))
    out = job.get("output", "output.root")
    seed = job.get("seed")
    gst_files = []
    energies = [math.sqrt(sum(c * c for c in mom)) / 1000.0   # |p|(MeV/c) -> GeV (nu massless)
                for _name, _pos, mom in entries]
    emax = spline_emax_gev(job, energies)
    print(f"[run_genie] per-event replay of {len(entries)} rays "
          f"({len(entries)} gevgen calls; this can be slow) ...", flush=True)
    for i, e_gev in enumerate(energies):
        ghep = workdir / f"_beam_{i}.ghep.root"
        gst = workdir / f"_beam_{i}.gst.root"
        cmd = ["gevgen", "-n", "1", "-p", str(job["probe"]), "-t", str(job["target"]),
               "--tune", job["tune"], "--event-generator-list", job["event_generator_list"],
               "-e", f"{e_gev:g}", "-o", str(ghep)] + _xsec_args(job, workdir, emax)
        if seed is not None:
            cmd += ["--seed", str(int(seed) + i)]
        subprocess.run(cmd, cwd=workdir, check=True)
        subprocess.run(["gntpc", "-i", str(ghep), "-f", "gst", "-o", str(gst)],
                       cwd=workdir, check=True)
        gst_files.append(str(gst))

    # The converter concatenates the per-ray gst files itself (no hadd needed).
    genie_convert.convert(gst_files, str(workdir / out),
                          vtx_units=job.get("length_units", "cm"),
                          beam=str(workdir / job["beam_file"]))
    print(f"[run_genie] done -> {workdir / out}", flush=True)
    return 0


GENIE_ENV_SCRIPT = "/opt/genie-env.sh"


def _enter_genie_env():
    """Merge the GENIE stack's environment into this process (merged image).

    In the combined GENIE+Geant4 image the GENIE environment (its own ROOT,
    Pythia6, LHAPDF, the HEDIS markers) is deliberately NOT global -- g4sim in
    the same image must keep the base ROOT, and ROOT sonames are unversioned.
    The driver owns every GENIE subprocess, so it enters that environment here:
    gevgen/gmkspl/gntpc/gmkhedissf children inherit it, and the driver's own
    GDMLTP_HEDIS / HEDIS_*_PATH lookups see it. A no-op where the script is
    absent (a standalone GENIE image bakes the env in globally instead).
    """
    if not os.path.exists(GENIE_ENV_SCRIPT):
        return
    dump = subprocess.run(
        ["bash", "-c", f"source {GENIE_ENV_SCRIPT} && env -0"],
        capture_output=True, check=True)
    for pair in dump.stdout.split(b"\0"):
        if b"=" in pair:
            key, _, val = pair.partition(b"=")
            os.environ[key.decode()] = val.decode(errors="replace")


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: run_genie.py <genie_job.json>", file=sys.stderr)
        return 2
    _enter_genie_env()
    return run(argv[0])


if __name__ == "__main__":
    sys.exit(main())
