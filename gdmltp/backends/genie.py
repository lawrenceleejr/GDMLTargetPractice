"""GENIE backend: turn the common RunConfig into a GENIE job spec.

The host writes a `genie_job.json` describing the probe, target, flux and run;
the GENIE container's driver reads it, runs the generation + conversion pipeline
(gevgen -> gntpc gst -> genie_convert), and writes the interaction record in the
shared schema. GENIE is a neutrino generator, so the projectile must be a
neutrino -- and it transports nothing, so this is stage 1 of two: the orchestrator
exports the final state to HepMC3 and Geant4 propagates it (see handoff.py).
`genie: {transport: false}` stops after this stage.
"""
import json
from pathlib import Path

from .base import Backend, Prepared
from ..config import ConfigError

GENIE_IMAGE = "ghcr.io/lawrenceleejr/gdmltargetpractice-genie:main"
JOB_FILE = "genie_job.json"
BEAM_FILE = "beam.dat"

# Geant4 particle names -> PDG (neutrinos only; GENIE is a neutrino generator).
_PROBE_PDG = {
    "nu_e": 12, "anti_nu_e": -12,
    "nu_mu": 14, "anti_nu_mu": -14,
    "nu_tau": 16, "anti_nu_tau": -16,
}

# Material-name -> struck-nucleus PDG for target inference. Users can always
# override with genie.target; molecular targets (e.g. water) collapse to their
# dominant heavy nucleus here and should be given explicitly for accuracy.
# Exact (whole-name) matches first, then unambiguous keyword substrings -- short
# fragments like "_c" are avoided because they false-match concrete/calcium/etc.
_NUCLEUS_ARGON = 1000180400
_NUCLEUS_O16 = 1000080160
_NUCLEUS_C12 = 1000060120
_NUCLEUS_SI = 1000140280
_NUCLEUS_FE = 1000260560
_NUCLEUS_H1 = 1000010010

_MATERIAL_EXACT = {
    "g4_c": _NUCLEUS_C12, "c": _NUCLEUS_C12,
    "g4_si": _NUCLEUS_SI, "si": _NUCLEUS_SI,
    "g4_ar": _NUCLEUS_ARGON, "g4_lar": _NUCLEUS_ARGON, "lar": _NUCLEUS_ARGON,
    "g4_fe": _NUCLEUS_FE, "fe": _NUCLEUS_FE,
    "g4_h": _NUCLEUS_H1,
}
_MATERIAL_KEYWORDS = [
    ("argon", _NUCLEUS_ARGON),
    ("water", _NUCLEUS_O16),
    ("graphite", _NUCLEUS_C12), ("carbon", _NUCLEUS_C12),
    ("silicon", _NUCLEUS_SI),
    ("stainless", _NUCLEUS_FE), ("steel", _NUCLEUS_FE), ("iron", _NUCLEUS_FE),
    ("hydrogen", _NUCLEUS_H1),
]


_UNIT_TO_GEV = {"ev": 1e-9, "kev": 1e-6, "mev": 1e-3, "gev": 1.0, "tev": 1e3}


def parse_energy_gev(s) -> float:
    """'2.0 GeV' -> 2.0 (GeV). A bare number is assumed to be GeV."""
    parts = str(s).split()
    val = float(parts[0])
    if len(parts) > 1:
        val *= _UNIT_TO_GEV.get(parts[1].lower(), 1.0)
    return val


def flux_gevgen_args(flux: dict):
    """Map the common energy spec to gevgen -e/-f arguments.

    Exact mappings: `mono` (single energy), `exp` and `mudecay_*` (functional
    spectra over a range -- gevgen -f takes a TF1 expression). `gauss`/`arb`
    are approximated by their nominal energy (a faithful histogram flux driver
    is a documented follow-up); the second return value flags whether the
    mapping is approximate so the caller can warn.
    """
    mode = flux.get("mode", "mono")
    if mode == "mono":
        return ["-e", f"{parse_energy_gev(flux['value']):g}"], False
    if mode == "exp":
        e0 = parse_energy_gev(flux["value"])
        emin = parse_energy_gev(flux["min"]); emax = parse_energy_gev(flux["max"])
        return ["-e", f"{emin:g},{emax:g}", "-f", f"exp(-x/{e0:g})"], False
    # JTR: Adding gsimple flux capabilities
    if mode == "flux":
        print(f"flux keys {flux.keys()}")
        print("Crashes now")
        return ["-f", flux["file"]], False
    if mode in ("mudecay_numu", "mudecay_nue", "mudecay_e"):
        # Angle-integrated lab spectrum of the daughters of in-flight muon decay
        # (value = parent muon energy). Exact TF1 expressions in y = x/E_mu.
        # mudecay_e shares the nu_mu shape (same rest-frame Michel spectrum);
        # GENIE itself only takes a neutrino probe, but the mapping stays exact
        # for any caller that reaches here.
        emu = parse_energy_gev(flux["value"])
        y = f"(x/{emu:g})"
        if mode == "mudecay_nue":
            expr = f"2.-6.*pow({y},2)+4.*pow({y},3)"
        else:
            expr = f"5./3.-3.*pow({y},2)+4./3.*pow({y},3)"
        emin = max(0.1, 1e-3 * emu)      # gevgen needs a nonzero lower edge
        return ["-e", f"{emin:g},{emu:g}", "-f", expr], False
    return ["-e", f"{parse_energy_gev(flux['value']):g}"], True


def flux_emax_gev(flux: dict) -> float:
    """Upper edge of the flux in GeV -- what the cross-section splines must
    reach. gauss has no hard edge; 5 sigma covers it for spline purposes."""
    mode = flux.get("mode", "mono")
    if mode == "exp":
        return parse_energy_gev(flux["max"])
    if mode == "arb":
        return max(parse_energy_gev(b["value"]) for b in flux["bins"])
    if mode == "gauss":
        return parse_energy_gev(flux["value"]) + 5.0 * parse_energy_gev(flux.get("sigma") or 0)
    return parse_energy_gev(flux["value"])   # mono, mudecay_* (value = E_mu)


_NEUTRINO_PDGS = {12, -12, 14, -14, 16, -16}


def probe_pdg(particle) -> int:
    """Resolve the GENIE probe from a neutrino name or a PDG id. GENIE is a
    neutrino generator, so a non-neutrino projectile is an error."""
    pdg = _as_pdg(particle)
    if pdg is not None:
        if pdg not in _NEUTRINO_PDGS:
            raise ConfigError(
                f"the genie backend only supports neutrino projectiles, got PDG {pdg} "
                f"(one of {sorted(_NEUTRINO_PDGS)})")
        return pdg
    try:
        return _PROBE_PDG[particle]
    except KeyError:
        raise ConfigError(
            f"the genie backend only supports neutrino projectiles, got {particle!r} "
            f"(names: {', '.join(sorted(_PROBE_PDG))}, or a neutrino PDG id)")


def _as_pdg(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.lstrip("-").isdigit():
        return int(v)
    return None


def _nucleus_for_material(mat: str):
    m = (mat or "").lower().strip()
    if m in _MATERIAL_EXACT:
        return _MATERIAL_EXACT[m]
    for needle, pdg in _MATERIAL_KEYWORDS:
        if needle in m:
            return pdg
    return None


def _primitive_size(p):
    """A crude volume proxy (product of the extent params) to pick the target
    volume over small structural pieces like windows/vessels."""
    pm = p.params or {}
    # AABB-bearing types (box/bbox/mesh) carry sx/sy/sz; cx/cy/cz are CENTER
    # offsets, not sizes, so multiply only the extents.
    if {"sx", "sy", "sz"} <= set(pm):
        return abs(pm["sx"]) * abs(pm["sy"]) * abs(pm["sz"]) or 1.0
    vals = [abs(float(v)) for v in pm.values() if isinstance(v, (int, float))]
    size = 1.0
    for v in vals:
        if v > 0:
            size *= v
    return size


def infer_target(gdml_path) -> int:
    """Infer the struck nucleus from the largest recognized non-world volume."""
    from .. import geometry
    prims = geometry.parse_gdml(str(gdml_path), include_world=True)
    candidates = []
    for p in prims:
        if getattr(p, "is_world", False):
            continue
        nucleus = _nucleus_for_material(getattr(p, "material", ""))
        if nucleus is not None:
            candidates.append((_primitive_size(p), nucleus, p.material))
    if not candidates:
        raise ConfigError(
            "could not infer a GENIE target nucleus from the geometry; "
            "set genie.target explicitly (e.g. 1000180400 for Ar-40)")
    candidates.sort(reverse=True)
    return candidates[0][1]


class GenieBackend(Backend):
    name = "genie"
    default_image = GENIE_IMAGE

    def prepare(self, cfg, outdir, image=None) -> Prepared:
        if not cfg.gdml:
            raise ConfigError("the genie backend requires geometry.gdml")
        outdir = Path(outdir)

        target = cfg.genie.get("target")
        if target in (None, "", "auto"):
            target = infer_target(cfg.gdml)

        e = cfg.beam.energy
        job = {
            "generator": "genie",
            "gdml": Path(cfg.gdml).name,
            "probe": probe_pdg(cfg.beam.particle),
            "target": int(target),
            "flux": {
                "mode": e.mode,
                "value": e.value,
                "min": e.min,
                "max": e.max,
                "bins": e.bins,
            },
            "position": cfg.beam.position,
            "direction": cfg.beam.direction,
            "flux_emax_gev": flux_emax_gev({
                "mode": e.mode, "value": e.value, "sigma": e.sigma,
                "min": e.min, "max": e.max, "bins": e.bins}),
            "events": int(cfg.run.events),
            "output": cfg.run.output,
            "tune": cfg.genie.get("tune", "G18_10a_00_000"),
            "cross_sections": cfg.genie.get("cross_sections", "auto"),
            "event_generator_list": cfg.genie.get("event_generator_list", "Default"),
            "length_units": cfg.genie.get("length_units", "cm"),
            "seed": cfg.run.seed,
        }

        # Host-sampled distributions / Twiss -> a per-event beam file the driver
        # replays (one gevgen call per ray, vertex + direction applied by the
        # converter). Overrides the aggregate flux above. Pure spectral modes
        # (incl. mudecay_*) stay on the fast native gevgen flux -- only actual
        # phase-space distributions force the per-event path.
        if cfg.beam.needs_phase_space_sampling():
            from .. import beam as beammod
            s = beammod.sample(cfg, int(cfg.run.events), seed=cfg.run.seed)
            beammod.write_beam_file(s, outdir / BEAM_FILE)
            job["beam_file"] = BEAM_FILE

        (outdir / JOB_FILE).write_text(json.dumps(job, indent=2) + "\n")

        return Prepared(argv=[JOB_FILE],
                        image=image or self.image_for(cfg),
                        env={}, output=cfg.run.output)
