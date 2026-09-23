"""Run a simulation with minimal friction.

This module is the backend-agnostic orchestrator: it takes a validated RunConfig,
asks the selected backend to render its inputs into the run directory, then wraps
`docker run` (or a local engine) around them. Each backend produces the same
`output.root` schema, so everything downstream (analyze/display/compare/info)
is generator-independent.

`generate_macro` and `run` keep their historical signatures for existing callers
and tests; both now delegate to the RunConfig/backend machinery.
"""
import collections
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import config, backends, handoff, warnings as gwarn
from .backends.geant4 import DEFAULT_IMAGE, build_macro

# Geant4 prints "--> Event N" (or "Event N starts") per printProgress modulo;
# we parse N to drive a host-side progress bar over the requested event count.
_EVENT_RE = re.compile(r"[Ee]vent[:\s]+(\d+)")

__all__ = ["DEFAULT_IMAGE", "generate_macro", "run", "run_config", "transport"]


# --------------------------------------------------------------------------- #
# Back-compat helpers
# --------------------------------------------------------------------------- #
def generate_macro(gdml, particle="e-", energy="1 GeV", position="0 0 -20 cm",
                   direction="0 0 1", n=100, nmode="auto", field=None):
    """Render a Geant4 macro from the classic mono-energy flag set.

    Retained for callers/tests that build a macro directly; internally it is just
    a thin adapter over the geant4 backend's `build_macro`.
    """
    cfg = config.RunConfig(
        generator="geant4", gdml=gdml,
        beam=config.Beam(particle=particle,
                         energy=config.Energy(mode="mono", value=energy),
                         position=position, direction=direction),
        run=config.RunSettings(events=n),
        geant4={"neutrino_mode": nmode, "field": field})
    return build_macro(cfg)


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
def _stage_gdml(cfg, outdir):
    """Copy the geometry next to the run so the in-container path is a basename."""
    if not cfg.gdml:
        return
    gsrc = Path(cfg.gdml)
    if gsrc.exists() and gsrc.resolve().parent != outdir.resolve():
        shutil.copy(gsrc, outdir / gsrc.name)


def _docker_user_args():
    """Run the container as the invoking host user, so files written into the
    mounted directory are owned by that user rather than root.

    GDMLTP_DOCKER_USER overrides: unset -> the current uid:gid (POSIX); "" or
    "root" -> the image default (root, the old behavior); "<uid>[:<gid>]" -> as
    given. On non-POSIX hosts (no os.getuid) Docker Desktop maps ownership
    itself, so nothing is added."""
    val = os.environ.get("GDMLTP_DOCKER_USER")
    if val is not None:
        val = val.strip()
        return [] if val in ("", "root") else ["--user", val]
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return []
    return ["--user", f"{getuid()}:{os.getgid()}"]


def _local_cmd(argv):
    """The command to run an engine stage WITHOUT spawning a container -- either
    on a dev host (g4sim on PATH) or, crucially, INSIDE a generator image so a
    single `docker run <image> run --config x.yaml --local` renders and runs
    end-to-end with no nested docker. Inside an image we defer to
    /app/entrypoint.sh, which dispatches the rendered input to the right engine
    (*.mac -> g4sim, *.json -> the generator driver) exactly as a bare
    `docker run` would."""
    entry = "/app/entrypoint.sh"
    if os.path.exists(entry):
        return [entry, *argv]
    exe = shutil.which("g4sim") or "/app/build/g4sim"   # host dev fallback (geant4)
    return [exe, *argv]


def _exec_stage(argv, image, env, outdir, local, dry_run, label="", events=None):
    """Run one container (or local-engine) stage; returns True if executed.

    Engine output is streamed live (with a tqdm progress bar over `events` when
    the engine reports per-event progress), so a run never sits silent; failures
    are turned into clear messages instead of raw tracebacks.
    """
    if local:
        cmd = _local_cmd(argv)
        run_env = dict(os.environ)
        run_env.update(env)
        if dry_run:
            print(f"[gdmltp] (dry-run{label})", " ".join(cmd), "in", str(outdir))
            return False
        print(f"[gdmltp] running the in-container engine{label} in {outdir}; "
              f"output follows:", flush=True)
        _stream_run(cmd, image=cmd[0], cwd=outdir, env=run_env,
                    events=events, is_docker=False)
    else:
        user = _docker_user_args()
        cmd = ["docker", "run", "--rm", "--init", *user,
               "-v", f"{outdir}:/run", "-w", "/run"]
        run_env = dict(env)
        if user:
            # a non-root uid has no home in the image; give caches (matplotlib,
            # ROOT) a writable one so they don't warn/fail
            run_env.setdefault("HOME", "/tmp")
        for k, v in run_env.items():
            cmd += ["-e", f"{k}={v}"]
        cmd += [image, *argv]
        if dry_run:
            print(f"[gdmltp] (dry-run{label})", " ".join(cmd))
            return False
        print(f"[gdmltp] launching container{label}:\n    {image}\n"
              f"[gdmltp] (first run pulls the image -- this can take a few minutes "
              f"with no output); engine output follows:", flush=True)
        _stream_run(cmd, image=image, cwd=None, env=None,
                    events=events, is_docker=True)
    return True


def _open_progress(events, label):
    """A tqdm bar over `events`, or None (no tqdm / no count / non-positive)."""
    if not events or events <= 0:
        return None
    try:
        from tqdm import tqdm
    except ImportError:
        return None
    return tqdm(total=int(events), desc=f"[gdmltp] transport{label}".strip(),
                unit="evt", dynamic_ncols=True, mininterval=0.5)


def _stream_run(cmd, image, cwd, env, events=None, is_docker=True, label=""):
    """Popen a stage, stream its output live, drive a progress bar off per-event
    lines, keep a tail for diagnostics, and raise a clear error on failure."""
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, text=True, bufsize=1,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except FileNotFoundError:
        if is_docker:
            raise RuntimeError(
                "docker was not found on PATH. Install Docker, or use --local to "
                "run a g4sim already on PATH.")
        raise RuntimeError("g4sim was not found on PATH (needed for --local).")

    tail = collections.deque(maxlen=40)
    bar = _open_progress(events, label)
    t0 = time.perf_counter()
    emit = bar.write if bar is not None else (lambda s: print(s, flush=True))
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            tail.append(line)
            m = bar is not None and _EVENT_RE.search(line)
            if m:
                n = int(m.group(1)) + 1
                if n > bar.n:
                    bar.update(min(n, bar.total) - bar.n)
            else:
                emit(line)
    finally:
        proc.wait()
        if bar is not None:
            if proc.returncode == 0:
                bar.update(bar.total - bar.n)
            bar.close()

    if proc.returncode == 0:
        print(f"[gdmltp] stage finished in {time.perf_counter() - t0:.1f}s", flush=True)
        return

    joined = "\n".join(tail)
    low = joined.lower()
    if is_docker and (proc.returncode == 125 or "manifest unknown" in low
                      or "pull access denied" in low or "unauthorized" in low
                      or "no such image" in low):
        raise RuntimeError(
            f"could not obtain the container image:\n    {image}\n"
            f"This usually means the image tag is not published or the package is "
            f"private. Try one of:\n"
            f"  - authenticate:  docker login ghcr.io\n"
            f"  - pick a tag that exists (branch builds are tagged by branch name), "
            f"e.g.  --image <repo>:<tag>\n"
            f"  - make the GHCR package public, or merge to the default branch so "
            f"the ':main'/':latest' tag publishes")
    if "command not found" in low:
        # a macro command the CLI emitted is absent from the engine -> the image
        # is older than this gdmltp checkout (a new /gun or /gdmltp command was
        # added after the image was built)
        cmd_line = next((l for l in tail if "COMMAND NOT FOUND" in l), "")
        raise RuntimeError(
            f"the engine rejected a command this gdmltp emitted:\n    {cmd_line.strip()}\n"
            f"The container image is OLDER than your gdmltp checkout -- it was "
            f"built before that command existed. Use an image built from your "
            f"current commit:\n"
            f"  - pull the branch tag (updated on every push), not a pinned "
            f"'-<sha>' tag:  --image {_repo_of(image)}:<branch>\n"
            f"  - or build it locally:  docker build -f docker/geant4.Dockerfile "
            f"-t g4sim-local . && gdmltp run ... --image g4sim-local")
    tail_txt = "\n".join(list(tail)[-15:]) or "(no output captured)"
    raise RuntimeError(
        f"the '{image}' stage exited with status {proc.returncode}:\n{tail_txt}")


def _repo_of(image):
    return image.rsplit(":", 1)[0] if ":" in image else image


def warn_about(cfg):
    """Loud banners for configurations that quietly produce the wrong physics.

    Emitted for real runs AND dry-runs (the advice is about the config, not the
    execution), before anything is launched, so the user sees it up front.
    """
    pdg = cfg.beam.pdg_code()
    if cfg.generator == "geant4" and pdg in gwarn.NEUTRINO_PDGS and not cfg.mac:
        gwarn.neutrino_on_geant4(cfg.beam.particle if cfg.beam.pdg is None
                                 else f"PDG {pdg}")
    if cfg.generator in config.VERTEX_LEVEL_GENERATORS and not _wants_transport(cfg):
        gwarn.generator_without_transport(cfg.generator)


def _wants_transport(cfg):
    """Geant4 transport is the default for every vertex-level generator: the
    common output.root is meant to carry a Geant4 transport record whatever
    produced the interaction. `<backend>: {transport: false}` opts out and
    leaves the run vertex-level."""
    if cfg.generator not in config.VERTEX_LEVEL_GENERATORS:
        return False
    return bool(getattr(cfg, cfg.generator).get("transport", True))


def _geant4_engine_here():
    """True when g4sim can run in THIS process's environment -- inside the
    Geant4 image, or on a dev host with g4sim on PATH."""
    return bool(shutil.which("g4sim")) or os.path.exists("/app/build/g4sim")


def _here(outdir):
    """`outdir` as the user would type it now (so a suggested command works)."""
    try:
        return os.path.relpath(outdir, Path.cwd())
    except ValueError:                      # different drive (Windows)
        return str(outdir)


def _no_engine_error(outdir, image):
    """The generator ran but Geant4 cannot: say so, and give the command that
    finishes the run. Worded for where we actually are -- inside a generator
    image (no g4sim in it) or on a host asked for --local without a build."""
    in_image = os.path.exists("/app/entrypoint.sh")
    why = ("this image ships no Geant4 engine" if in_image else
           "there is no g4sim on PATH for --local")
    return RuntimeError(
        f"the generator stage finished, but {why}, so the transport stage "
        f"cannot run here.\n"
        f"Its inputs are ready in {outdir} ({handoff.EVENT_FILE}, "
        f"{handoff.STAGE_SPEC}); finish the run in a Geant4 image:\n"
        f"    docker run --rm --user \"$(id -u):$(id -g)\" -e HOME=/tmp \\\n"
        f"        -v \"$PWD:/run\" -w /run {image} transport -o {_here(outdir)}\n"
        + (f"or run the whole thing from the host front-end, which chains the "
           f"two images itself:\n"
           f"    gdmltp run --config <config> -o {_here(outdir)}\n"
           if in_image else
           f"or drop --local so the transport stage runs in {image}.\n")
        + f"(add --stage generator to make stopping here the intended outcome "
          f"rather than an error)")


def _transport_stage(cfg, prep, outdir, local, dry_run, stage="full"):
    """Stage 2 of the generator->Geant4 hand-off: replay the generator's final
    state through g4sim via HepMC3 (fills step_*/totalEdep/trk_end*), then
    graft the generator's nu_* block + primary identity back on."""
    from .backends.geant4 import Geant4Backend

    image = Geant4Backend().image_for(cfg)
    if dry_run:
        print(f"[gdmltp] (dry-run:handoff) would export the generator events to "
              f"{handoff.EVENT_FILE} (HepMC3)")
        if stage == "generator":
            print(f"[gdmltp] (dry-run) would stop there; `gdmltp transport -o "
                  f"{_here(outdir)}` finishes the run in {image}")
        else:
            print(f"[gdmltp] (dry-run:transport) would replay them through "
                  f"{image} via /gun/hepmcFile, merging the nu_* block into "
                  f"{cfg.run.output}")
        return

    spec = handoff.stage_inputs(
        outdir, Path(cfg.gdml).name, output=cfg.run.output, seed=cfg.run.seed,
        field=cfg.geant4.get("field"), generator=cfg.generator,
        produced=prep.output, image=image)
    print(f"[gdmltp] hand-off: {spec['events']} event(s) -> {handoff.EVENT_FILE} "
          f"(HepMC3, the generator -> Geant4 interchange)")

    if stage == "generator":
        print(f"[gdmltp] stopping after the generator stage as asked. Finish the "
              f"run in a Geant4 image:\n"
              f"    gdmltp transport -o {_here(outdir)}   "
              f"(-> {outdir / cfg.run.output})")
        return
    if local and not _geant4_engine_here():
        raise _no_engine_error(outdir, image)
    _run_transport_spec(spec, outdir, image, local)


def _run_transport_spec(spec, outdir, image, local):
    """Execute a stage-2 spec: g4sim on the HepMC3 file, then the merge."""
    n = int(spec["events"])
    print(f"[gdmltp] transport: replaying {n} generator event(s) through Geant4 ...")
    env = {"CELER_DISABLE": "1"} if spec.get("field") else {}
    _exec_stage([spec["macro"]], image, env, outdir, local, dry_run=False,
                label=":transport", events=n)

    transported = outdir / "output.root"
    target = outdir / spec["output"]
    handoff.merge_nu_block(transported, outdir / spec["vertex"], target)
    if target != transported and transported.exists():
        transported.unlink()
    print(f"[gdmltp] transport done -> {target} "
          f"(generator interaction record + Geant4 transport)")


def transport(outdir=".", image=None, local=False, dry_run=False):
    """Run stage 2 alone, from the files a generator stage left in `outdir`.

    This is what `gdmltp transport` calls: it lets a Geant4 image finish a run
    that a generator image started (bare `docker run` cannot chain containers),
    and lets a transport be re-run without regenerating the events.
    """
    outdir = Path(outdir).resolve()
    spec = handoff.read_spec(outdir)
    from .backends.base import image_override
    image = image or image_override("geant4") or spec.get("image") or DEFAULT_IMAGE
    if not local and os.path.exists("/app/entrypoint.sh"):
        local = True                      # inside an image: run the engine here
    if local and not _geant4_engine_here():
        raise RuntimeError(
            f"no Geant4 engine here (no g4sim on PATH): `transport` must run in "
            f"a Geant4 image or on a host with g4sim built. From a host with "
            f"Docker, drop --local and it launches {image}.")
    if dry_run:
        print(f"[gdmltp] (dry-run:transport) would replay {spec['events']} "
              f"event(s) from {spec['event_file']} through {image} into "
              f"{spec['output']}")
        return 0
    _run_transport_spec(spec, outdir, image, local)
    return 0


def run_config(cfg, image=None, outdir=".", local=False, dry_run=False,
               stage="full"):
    """Execute (or dry-run) a run described by a validated RunConfig.

    For every vertex-level generator (genie/achilles/pythia/external) this is a
    two-stage run, and both stages run by default: the generator produces the
    interaction, its final state is exported as HepMC3, and g4sim transports it
    through the GDML detector, the two records merging into one output.root.
    `<backend>: {transport: false}` keeps a run vertex-level.

    stage="generator" stops after stage 1 with the hand-off written -- for the
    bare-`docker run` flow, where the generator image cannot start the Geant4
    container itself and `gdmltp transport` finishes the run in the Geant4 image.
    """
    cfg.validate()
    warn_about(cfg)
    outdir = Path(outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    # Inside a gdmltp engine image, run the engine here rather than spawning a
    # nested container -- so a single `docker run <image> run --config x.yaml`
    # renders inputs AND runs end-to-end. On a host (no /app/entrypoint.sh) this
    # is a no-op and runs default to Docker as before.
    if not local and os.path.exists("/app/entrypoint.sh"):
        local = True

    _stage_gdml(cfg, outdir)

    backend = backends.get(cfg.generator)
    prep = backend.prepare(cfg, outdir, image=image)

    if not cfg.mac and (outdir / "gdmltp_run.mac").exists() and cfg.generator == "geant4":
        print(f"[gdmltp] generated {outdir / 'gdmltp_run.mac'}:\n"
              f"{(outdir / 'gdmltp_run.mac').read_text()}")

    if backend.host:
        # host backends (external) produce their output in prepare(); there
        # is no container stage to run
        executed = True
    else:
        events = int(cfg.run.events) if cfg.generator in ("geant4", "decay") else None
        executed = _exec_stage(prep.argv, prep.image, prep.env, outdir, local,
                               dry_run, events=events)

    if executed and prep.post is not None and not dry_run:
        prep.post()

    if _wants_transport(cfg):
        _transport_stage(cfg, prep, outdir, local, dry_run, stage=stage)
        return 0
    if stage == "generator":
        if cfg.generator in config.VERTEX_LEVEL_GENERATORS:
            raise config.ConfigError(
                f"--stage generator splits a two-stage run, but this "
                f"{cfg.generator} run set transport: false, so there is no "
                f"Geant4 stage to split off")
        raise config.ConfigError(
            f"--stage generator splits a generator run into its two engines, "
            f"but the {cfg.generator} backend is a single Geant4 stage "
            f"(it transports what it makes)")

    if executed:
        _finalize_output(cfg, prep, outdir)
    return 0


def _finalize_output(cfg, prep, outdir):
    produced = outdir / prep.output
    target = outdir / cfg.run.output
    if cfg.run.output != prep.output and produced.exists():
        produced.replace(target)
    print(f"[gdmltp] done -> {target if target.exists() else produced}")


def run(mac=None, gdml=None, particle="e-", energy="1 GeV", position="0 0 -20 cm",
        direction="0 0 1", n=100, nmode="auto", field=None, image=DEFAULT_IMAGE,
        outdir=".", local=False, celer_disable=None, dry_run=False):
    """Historical flag-driven entry point (geant4). Builds a RunConfig and runs it."""
    cfg = config.RunConfig(
        generator="geant4", gdml=gdml, mac=mac,
        beam=config.Beam(particle=particle,
                         energy=config.Energy(mode="mono", value=energy),
                         position=position, direction=direction),
        run=config.RunSettings(events=n),
        geant4={"neutrino_mode": nmode, "field": field})
    return run_config(cfg, image=image, outdir=outdir, local=local, dry_run=dry_run)
