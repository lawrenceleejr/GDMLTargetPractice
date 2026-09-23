"""Generator -> Geant4 transport hand-off (the DUNE-style two-stage pattern).

GENIE, Achilles and Pythia generate the interaction and its nuclear exit state
but do not transport anything through the detector, so every one of those runs
finishes in Geant4: whatever the generator, its final state is exported as
HepMC3 and g4sim replays it through the GDML geometry, and the one common
`output.root` always carries a Geant4 transport record. This module bridges the
two halves:

  1. `write_event_file`  -- turn a vertex-level `output.root` (from
     genie_convert / achilles_convert / the external backend, which record
     every final-state particle's momentum in trk_px/py/pz) into a HepMC3
     ASCII event file, written with the official library (pyhepmc). HepMC3 is
     the single generator->Geant4 interchange format; g4sim reads it back with
     HepMC3::ReaderAscii and replays one multi-particle vertex per event
     (primaries stamped with the vertex time), filling step_*, totalEdep, and
     real trk_end* by transport. The vertex comes from nu_vertex* when present
     (neutrino generators) else primaryEnd*; the time from decayT when present.

  2. `merge_nu_block`   -- graft the generator's record (the nu_* block when
     present, the primary identity, the vertex-level primaryEnd*, and any
     optional scalars like eventWeight/decayT) onto the transported file, so
     the final output.root carries BOTH the generator-quality physics and the
     Geant4 transport record in the one common schema.

`stage_inputs` packages step 1 as files in the run directory -- the vertex-level
ntuple, `events.hepmc`, the g4sim macro and a small JSON spec -- so stage 2 is a
self-contained job: any Geant4 image can finish a run a generator image started
(`gdmltp transport -o <rundir>`), and the front-end chains the two itself.

Neutral final-state neutrinos are kept in the event file (Geant4 transports
them trivially; with biasing off they simply leave).
"""
import json
from pathlib import Path

import numpy as np
import uproot

from . import io

TRANSPORT_MACRO = "gdmltp_transport.mac"
EVENT_FILE = "events.hepmc"          # HepMC3 ASCII: the standard interchange
VERTEX_FILE = "vertex_level.root"
STAGE_SPEC = "gdmltp_transport.json"  # everything stage 2 needs, from stage 1

C_MM_PER_NS = 299.792458

# Branches replaced on the transported file by the generator's values: the
# interaction record, the primary identity (probe/parent -- matching what a
# native Geant4 run would record), and the vertex-level end point (interaction
# or decay vertex; the transported file's own primaryEnd* would describe the
# first daughter instead).
_MERGE_SCALARS = ["primaryPDG", "primaryE",
                  "primaryStartX", "primaryStartY", "primaryStartZ",
                  "primaryStartPx", "primaryStartPy", "primaryStartPz",
                  "primaryEndE", "primaryEndX", "primaryEndY", "primaryEndZ",
                  "primaryEndPx", "primaryEndPy", "primaryEndPz"]

# Optional per-event scalars carried over verbatim when the vertex-level file
# has them (decay backend: forced-decay weight + decay time).
_MERGE_OPTIONAL = ["eventWeight", "decayT"]


def write_event_file(vertex_root, path, tree="tree"):
    """Write the generator -> Geant4 hand-off as a HepMC3 ASCII file.

    HepMC3 is the standard interchange, written with the official library
    (pyhepmc), not a bespoke format: one GenEvent per ntuple entry, units
    MEV/MM (lossless with the schema), a single production vertex at the
    interaction/decay point carrying its time, the primary as an incoming
    status-4 beam particle (provenance), and every final-state trk_* as an
    outgoing status-1 particle. The event weight (eventWeight) becomes the
    HepMC weight. g4sim reads it back with HepMC3::ReaderAscii.
    """
    import pyhepmc
    from . import masses

    with uproot.open(vertex_root) as f:
        t = f[tree]
        names = {k.split(";")[0] for k in t.keys()}
        pdg = t["trk_pdg"].array()
        px, py, pz = (t[f"trk_p{a}"].array() for a in "xyz")
        kin = t["trk_startE"].array()
        p0pdg = t["primaryPDG"].array(library="np")
        p0 = {a: t[f"primaryStart{a.upper()}"].array(library="np") for a in "xyz"}
        p0p = {a: t[f"primaryStartP{a}"].array(library="np") for a in "xyz"}
        p0e = t["primaryE"].array(library="np")
        if "nu_vertexX" in names:
            vx, vy, vz = (t[f"nu_vertex{a}"].array(library="np") for a in "XYZ")
        else:
            vx, vy, vz = (t[f"primaryEnd{a}"].array(library="np") for a in "XYZ")
        tns = t["decayT"].array(library="np") if "decayT" in names else None
        wgt = t["eventWeight"].array(library="np") if "eventWeight" in names else None

    n_events = len(vx)
    with pyhepmc.open(path, "w") as writer:
        for i in range(n_events):
            ev = pyhepmc.GenEvent(pyhepmc.Units.MEV, pyhepmc.Units.MM)
            ev.event_number = i
            t_len = float(tns[i]) * C_MM_PER_NS if tns is not None else 0.0
            vtx = pyhepmc.GenVertex((float(vx[i]), float(vy[i]), float(vz[i]), t_len))
            m0 = masses.mass_mev(int(p0pdg[i]))
            beam = pyhepmc.GenParticle(
                (float(p0p["x"][i]), float(p0p["y"][i]), float(p0p["z"][i]),
                 float(p0e[i])), int(p0pdg[i]), 4)
            beam.generated_mass = m0
            vtx.add_particle_in(beam)
            for k in range(len(pdg[i])):
                pid = int(pdg[i][k])
                m = masses.mass_mev(pid)
                etot = float(kin[i][k]) + m
                fs = pyhepmc.GenParticle(
                    (float(px[i][k]), float(py[i][k]), float(pz[i][k]), etot), pid, 1)
                fs.generated_mass = m
                vtx.add_particle_out(fs)
            ev.add_vertex(vtx)
            if wgt is not None:
                ev.weights = [float(wgt[i])]
            writer.write(ev)
    return n_events


def read_event_file(path):
    """Read the hand-off HepMC3 file back into
    [(vertex(x,y,z) mm, [(pdg, (px,py,pz) MeV/c), ...])] -- the final-state
    (status-1) particles per event. Uses pyhepmc (the official reader)."""
    import pyhepmc
    events = []
    with pyhepmc.open(str(path)) as reader:
        for ev in reader:
            v = ev.vertices[0].position if ev.vertices else None
            vertex = (v.x, v.y, v.z) if v is not None else (0.0, 0.0, 0.0)
            parts = [(p.pid, (p.momentum.px, p.momentum.py, p.momentum.pz))
                     for p in ev.particles if p.status == 1]
            events.append((vertex, parts))
    return events


def build_transport_macro(gdml_name, n_events, event_file=EVENT_FILE, seed=None,
                          field=None):
    """The stage-2 g4sim macro: replay the event file through the detector.
    neutrinoMode is off -- the nu_* block comes from the generator via
    merge_nu_block, not from Geant4."""
    lines = [f"/detector/readGDML {gdml_name}", "/run/initialize",
             "/analysis/neutrinoMode off"]
    if seed is not None:
        lines.append(f"/random/setSeeds {int(seed)} {int(seed) + 1}")
    if field:
        lines.append(f"/detector/setGlobalField {field}")
    lines += [f"/gun/hepmcFile {event_file}",
              f"/run/printProgress {max(1, int(n_events) // 100)}",
              f"/run/beamOn {int(n_events)}"]
    return "\n".join(lines) + "\n"


def stage_inputs(outdir, gdml_name, output="output.root", seed=None, field=None,
                 generator="", produced="output.root", image=None):
    """Package stage 1's result as the stage-2 job, in the run directory.

    Renames the generator's ntuple to `vertex_level.root`, exports its final
    state to `events.hepmc` (the interchange g4sim reads), renders the transport
    macro, and writes the spec that `run_transport` replays. Returns the spec.
    """
    outdir = Path(outdir)
    vertex = outdir / VERTEX_FILE
    (outdir / produced).replace(vertex)

    n = write_event_file(vertex, outdir / EVENT_FILE)
    (outdir / TRANSPORT_MACRO).write_text(
        build_transport_macro(gdml_name, n, seed=seed, field=field))

    spec = {"generator": generator, "gdml": gdml_name, "events": int(n),
            "seed": seed, "field": field, "output": output,
            "vertex": VERTEX_FILE, "event_file": EVENT_FILE,
            "macro": TRANSPORT_MACRO,
            # the Geant4 image this run asked for (a celeritas variant, a pinned
            # tag), so a later `gdmltp transport` uses the same engine
            "image": image}
    (outdir / STAGE_SPEC).write_text(json.dumps(spec, indent=2) + "\n")
    return spec


def read_spec(outdir):
    """The stage-2 spec written by `stage_inputs`, with its files checked."""
    outdir = Path(outdir)
    path = outdir / STAGE_SPEC
    if not path.exists():
        raise FileNotFoundError(
            f"no {STAGE_SPEC} in {outdir}: nothing to transport there. That file "
            f"is written by the generator stage -- run `gdmltp run --config "
            f"<config> -o {outdir}` first (with a vertex-level generator).")
    spec = json.loads(path.read_text())
    for key in ("event_file", "macro", "vertex", "gdml"):
        missing = outdir / spec[key]
        if not missing.exists():
            raise FileNotFoundError(
                f"{STAGE_SPEC} refers to {spec[key]}, which is not in {outdir}")
    return spec


def merge_nu_block(transported_root, vertex_root, out_path, tree="tree"):
    """Write `out_path` = the transported tree with the generator's nu_* block
    and primary identity grafted on. Event counts must match 1:1."""
    with uproot.open(transported_root) as ft:
        tt = ft[tree]
        n_t = int(tt.num_entries)
        names = [k.split(";")[0] for k in tt.keys()]
        data = {name: tt[name].array() for name in names}

    with uproot.open(vertex_root) as fv:
        tv = fv[tree]
        n_v = int(tv.num_entries)
        if n_t != n_v:
            raise ValueError(
                f"transported file has {n_t} events but vertex-level has {n_v}; "
                f"did the transport run use /gun/hepmcFile with beamOn {n_v}?")
        vnames = {k.split(";")[0] for k in tv.keys()}
        for name in list(vnames):
            if name.startswith("nu_"):
                data[name] = tv[name].array()
        for name in _MERGE_SCALARS + _MERGE_OPTIONAL:
            if name in vnames:
                data[name] = tv[name].array(library="np")

    io.write_tree(out_path, data, tree=tree)
    return out_path
