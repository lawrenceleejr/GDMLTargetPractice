"""Common run configuration: a generator-agnostic front-end shared by every backend.

A run is described by a small tree of dataclasses (`RunConfig`) that can be built
two ways:

  * from a YAML file (`from_yaml`) -- the documented `--config run.yaml` frontend, or
  * from CLI flags (`from_flags`) -- the historical `gdmltp run --gdml ... --particle ...`.

`load` combines them with the precedence **CLI flag > YAML value > schema default**,
so `gdmltp run --config r.yaml --energy "200 MeV"` overrides just that one field.

The config is deliberately backend-agnostic in its core (`geometry`, `beam`, `run`);
backend-specific knobs live in namespaced blocks (`geant4:` / `genie:`) that the
other backend ignores. This is the single place YAML is parsed and validated, so
backends receive an already-validated object and never touch raw user input.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


GENERATORS = ("geant4", "genie", "achilles", "pythia", "decay", "external")

# Generators that produce an interaction but transport nothing: their final
# state is handed to Geant4 as HepMC3 unless `<backend>: {transport: false}`.
# decay is not one of them -- it is a single Geant4 stage that decays AND
# transports.
VERTEX_LEVEL_GENERATORS = ("genie", "achilles", "pythia", "external")
# mudecay_*: the DAUGHTER energy spectra from in-flight muon decay (the
# neutrino-factory / muon-collider "neutrino slice" flux, angle-integrated over
# the ~1/gamma cone; unpolarized). energy.value is the PARENT MUON energy E_mu:
#   mudecay_numu (nu_mu / anti-nu_mu): dN/dy = 5/3 - 3y^2 + 4/3 y^3, y = E/E_mu
#   mudecay_nue  (nu_e  / anti-nu_e ): dN/dy = 2 - 6y^2 + 4y^3
#   mudecay_e    (e- / e+, the Michel daughter): the SAME shape as mudecay_numu.
#     For an unpolarized muon the charged lepton and the nu_mu share the
#     rest-frame Michel spectrum 2x^2(3-2x), so they share the boosted one too:
#     <E> = 0.35 E_mu each, leaving the nu_e the remaining 0.30 E_mu.
ENERGY_MODES = ("mono", "flux", "gauss", "exp", "arb",
                "mudecay_numu", "mudecay_nue", "mudecay_e")
# Modes with no native g4sim gun command: the host samples them into a beam file.
SAMPLED_ENERGY_MODES = ("mudecay_numu", "mudecay_nue", "mudecay_e")

# Pythia 8 process presets (pythia.process). Each expands to a small set of
# Pythia settings in the backend; raw pythia.settings are appended after them so
# a user can always override or add anything the presets don't cover.
#   dis      -- deep-inelastic scattering off a nucleon via weak-boson exchange
#               (CC W + NC gamma/Z): the lepton/neutrino-beam case, valid to TeV
#               and beyond with no cross-section splines to precompute
#   softqcd  -- inelastic minimum-bias hadron-nucleon collisions
#   hardqcd  -- hard QCD 2->2 jets (needs a pTmin cut)
#   none     -- no preset; pythia.settings (or a verbatim cmnd) is the whole card
PYTHIA_PROCESSES = ("dis", "softqcd", "hardqcd", "none")


class ConfigError(ValueError):
    """A user-facing configuration problem (bad/missing field). cli.py renders
    these as a friendly one-liner (they are a subclass of ValueError)."""


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
@dataclass
class Energy:
    """Beam energy spectrum. Values are strings with a unit (e.g. "150 MeV"),
    passed verbatim to the backend just like the Geant4 macro expects."""
    mode: str = "mono"            # mono | gauss | exp | arb
    value: str = "1 GeV"
    sigma: Optional[str] = None  # gauss only
    min: Optional[str] = None    # exp / arb range
    max: Optional[str] = None    # exp / arb range
    bins: list = field(default_factory=list)  # arb: list of {"value","weight"}


# JTR: Adding new dataclass for providing flux information to GENIE
# TODO: Add more guardrails to prevent misuse
@dataclass
class Flux:
    """Flux file capability, specifically for GENIE"""
    file: Optional[str] = None
    mode: str = "gsimple"


DIST_KINDS = ("fixed", "gauss", "uniform")


def _validate_dist(d, name):
    if d.kind not in DIST_KINDS:
        raise ConfigError(f"{name}: unknown distribution {d.kind!r}; "
                          f"choose one of {', '.join(DIST_KINDS)}")
    if d.kind == "fixed" and d.value is None:
        raise ConfigError(f"{name}: fixed distribution needs a value")
    if d.kind == "gauss" and d.sigma is None:
        raise ConfigError(f"{name}: gauss distribution needs sigma (mean defaults to 0)")
    if d.kind == "uniform" and (d.min is None or d.max is None):
        raise ConfigError(f"{name}: uniform distribution needs min and max")


@dataclass
class Distribution:
    """A 1-D distribution for a single beam coordinate. A bare scalar-with-unit
    in the YAML means `fixed`; a mapping selects gauss/uniform. Values are
    unit-carrying strings (e.g. "2 mm", "5 mrad", "10 MeV/c")."""
    kind: str = "fixed"                 # fixed | gauss | uniform
    value: Optional[str] = None         # fixed
    mean: Optional[str] = None          # gauss
    sigma: Optional[str] = None         # gauss
    min: Optional[str] = None           # uniform
    max: Optional[str] = None           # uniform

    def is_fixed(self):
        return self.kind == "fixed"


@dataclass
class TwissPlane:
    alpha: float = 0.0
    beta: float = 1.0        # [m]
    emittance: float = 0.0   # geometric, [mm.mrad]


@dataclass
class Twiss:
    """Correlated phase space at a reference point (geometric emittance,
    mm.mrad; beta in m). Sampled as a correlated Gaussian from the beam matrix."""
    x: TwissPlane = field(default_factory=TwissPlane)
    y: TwissPlane = field(default_factory=TwissPlane)
    p0: str = "1 GeV/c"      # reference momentum magnitude
    dp_over_p: float = 0.0   # fractional momentum spread (gaussian sigma)
    ref_position: str = "0 0 0 mm"
    ref_direction: str = "0 0 1"


@dataclass
class Beam:
    particle: str = "e-"     # Geant4 name, or str(pdg) when pdg-defined (display)
    pdg: Optional[int] = None  # set when the projectile is given by PDG id
    mass: Optional[str] = None  # rest mass override, e.g. "1.0 GeV" -- required
                                # for BSM projectiles whose PDG id is not in the
                                # common mass table (HNLs, dark photons, ...)
    flux: Optional[Flux] = field(default_factory=Flux)
    energy: Optional[Energy] = field(default_factory=Energy)
    position: str = "0 0 -20 cm"
    direction: str = "0 0 1"     # "0 0 0" -> isotropic (geant4 only)
    angle_sigma: Optional[str] = None  # gaussian angular cone spread, e.g. "10 deg"
    # --- distribution / phase-space extensions (host-sampled -> beam file) ---
    position_dist: Optional[dict] = None      # {"x":Distribution,"y":...,"z":...}
    direction_slopes: Optional[dict] = None   # {"xprime":Distribution,"yprime":Distribution}
    momentum: Optional[Distribution] = None   # |p| spectrum (MeV/c); alt to energy
    twiss: Optional[Twiss] = None

    def is_pdg(self) -> bool:
        """The projectile was given by PDG id rather than a Geant4 name."""
        return self.pdg is not None

    def pdg_code(self):
        """Resolve to a PDG code: the explicit id, else a lookup of the name in
        the common particle table, else None (name not in the small table)."""
        if self.pdg is not None:
            return self.pdg
        from . import particles
        return particles.pdg_for(self.particle)

    def identifier(self) -> str:
        """Beam-file / display token: the PDG integer (pdg-defined) or the name."""
        return str(self.pdg) if self.pdg is not None else self.particle

    def needs_phase_space_sampling(self) -> bool:
        """Position/direction/momentum distributions or Twiss: per-event values
        no engine can produce from single-value gun/flux settings."""
        return bool(self.position_dist or self.direction_slopes
                    or self.momentum or self.twiss)

    def needs_sampling(self) -> bool:
        """True when the beam must be sampled host-side into a beam file (the
        engines can't produce these per-event distributions from single-value
        gun commands). Plain energy modes + a fixed position/direction (+ cone)
        stay on the analytic macro path. Backends with a native spectral flux
        (GENIE's functional -f) may use needs_phase_space_sampling() instead and
        map SAMPLED_ENERGY_MODES themselves."""
        return (self.needs_phase_space_sampling()
                or self.energy.mode in SAMPLED_ENERGY_MODES)


@dataclass
class RunSettings:
    events: int = 100
    output: str = "output.root"
    seed: Optional[int] = None


@dataclass
class RunConfig:
    generator: str = "geant4"
    gdml: Optional[str] = None
    mac: Optional[str] = None    # geant4 escape hatch: run this macro verbatim
    beam: Beam = field(default_factory=Beam)
    run: RunSettings = field(default_factory=RunSettings)
    geant4: dict = field(default_factory=dict)  # field, neutrino_mode, celeritas, physics_list
    genie: dict = field(default_factory=dict)   # tune, cross_sections, target, ...
    achilles: dict = field(default_factory=dict)  # nuclear_model, cascade, processes, run_card, ...
    pythia: dict = field(default_factory=dict)  # process, settings, target, cmnd, transport, ...
    decay: dict = field(default_factory=dict)   # ctau, ctau_sample, channels, charge, name
    external: dict = field(default_factory=dict)  # file, format, transport

    def validate(self):
        if self.generator not in GENERATORS:
            raise ConfigError(
                f"unknown generator {self.generator!r}; choose one of {', '.join(GENERATORS)}")
        if not self.gdml and not self.mac:
            raise ConfigError("no geometry: set geometry.gdml (or, for geant4, a macro via --mac)")
        if self.mac and self.generator != "geant4":
            raise ConfigError("a verbatim macro (mac) is only meaningful for the geant4 backend")
        if self.beam.pdg is not None and self.beam.pdg == 0:
            raise ConfigError("beam.pdg must be a nonzero PDG id")
        e = self.beam.energy
        if e.mode not in ENERGY_MODES:
            raise ConfigError(
                f"unknown energy mode {e.mode!r}; choose one of {', '.join(ENERGY_MODES)}")
        if e.mode == "gauss" and not e.sigma:
            raise ConfigError("energy mode 'gauss' requires energy.sigma")
        if e.mode == "exp" and not (e.min and e.max):
            raise ConfigError("energy mode 'exp' requires energy.min and energy.max")
        if e.mode == "arb" and not e.bins:
            raise ConfigError("energy mode 'arb' requires a non-empty energy.bins list")
        nm = self.geant4.get("neutrino_mode")
        if nm is not None and nm not in ("auto", "on", "off"):
            raise ConfigError(
                f"geant4.neutrino_mode must be auto/on/off, got {nm!r}")
        self._validate_beam_distributions()
        self._validate_exit_hepmc()
        self._validate_transport()
        if self.generator == "decay":
            self._validate_decay()
        if self.generator == "external":
            self._validate_external()
        if self.generator == "pythia":
            self._validate_pythia()
        try:
            int(self.run.events)
        except (TypeError, ValueError):
            raise ConfigError(f"run.events must be an integer, got {self.run.events!r}")
        return self

    def _validate_exit_hepmc(self):
        """`geant4.exit_hepmc` and friends: the HepMC3 export of what leaves a
        volume. Catch the typos here rather than in a Geant4 macro error."""
        g = self.geant4
        path = g.get("exit_hepmc")
        if path is None:
            for key in ("exit_volume", "exit_min_ke", "exit_kill"):
                if key in g:
                    raise ConfigError(
                        f"geant4.{key} only means something with "
                        f"geant4.exit_hepmc set (the file to write)")
            return
        if not isinstance(path, str) or not path.strip():
            raise ConfigError("geant4.exit_hepmc must be a file name, e.g. exit.hepmc")
        if "exit_kill" in g and not isinstance(g["exit_kill"], bool):
            raise ConfigError(
                f"geant4.exit_kill must be true or false, got {g['exit_kill']!r}")
        ke = g.get("exit_min_ke")
        if ke is not None:
            # reuse the beam parser so the accepted spellings are identical to
            # every other energy in a config
            from .beam import _ene_mev
            try:
                _ene_mev(ke)
            except (ValueError, TypeError, IndexError):
                raise ConfigError(
                    f"geant4.exit_min_ke must be an energy with a unit "
                    f'(e.g. "1 MeV"), got {ke!r}')

    def _validate_transport(self):
        """Geant4 transport of a generator's final state is ON by default (the
        common output.root is meant to carry a transport record whatever made
        the interaction); `transport: false` is the explicit opt-out, so the
        value has to be a real boolean rather than, say, the string "no"."""
        if self.generator not in VERTEX_LEVEL_GENERATORS:
            return
        block = getattr(self, self.generator)
        if "transport" in block and not isinstance(block["transport"], bool):
            raise ConfigError(
                f"{self.generator}.transport must be true or false, got "
                f"{block['transport']!r}. Geant4 transport is the default; set "
                f"it to false for a vertex-level (generator-only) run.")

    def _validate_decay(self):
        """Structural checks for the decay backend. The decay itself is done
        by GEANT4 (g4sim/BSMPhysics: G4DecayTable + G4PhaseSpaceDecayChannel +
        G4Decay); this framework only defines the particle and reweights."""
        d = self.decay
        if not d.get("ctau") and not d.get("lifetime"):
            raise ConfigError("the decay backend requires decay.ctau "
                              "(e.g. \"10 m\") or decay.lifetime (e.g. \"1 ns\")")
        channels = d.get("channels")
        if not channels or not isinstance(channels, list):
            raise ConfigError("the decay backend requires decay.channels: a list "
                              "of {to: [pdg, ...], br: <fraction>} mappings")
        for i, ch in enumerate(channels):
            if not isinstance(ch, dict) or not isinstance(ch.get("to"), list) \
                    or not (2 <= len(ch["to"]) <= 4):
                raise ConfigError(f"decay.channels[{i}] needs 'to': a list of 2-4 "
                                  f"daughter PDG ids (Geant4's phase-space "
                                  f"channel supports 2-4 bodies)")
            try:
                [int(p) for p in ch["to"]]
            except (TypeError, ValueError):
                raise ConfigError(f"decay.channels[{i}].to must contain PDG ids")
            if float(ch.get("br", 1.0)) <= 0:
                raise ConfigError(f"decay.channels[{i}].br must be > 0")
            if "model" in ch:
                raise ConfigError(
                    f"decay.channels[{i}].model is not supported: Geant4 decays "
                    f"with its phase-space channel. For matrix-element decay "
                    f"distributions generate events with a real generator "
                    f"(Pythia8/MadGraph) and use the 'external' backend")
        if "fiducial" in d:
            raise ConfigError(
                "decay.fiducial was replaced by lifetime importance sampling: "
                "set decay.ctau_sample to a detector-scale value and each event "
                "gets an exact eventWeight back to the true decay.ctau")
        if "transport" in d:
            raise ConfigError(
                "decay.transport is implicit: the decay backend is a single "
                "Geant4 stage that decays AND transports")

    def _validate_external(self):
        x = self.external
        if not x.get("file"):
            raise ConfigError("the external backend requires external.file: "
                              "a HepMC3 ASCII event file (e.g. from Pythia8)")
        fmt = x.get("format", "hepmc3")
        if fmt != "hepmc3":
            raise ConfigError(f"external.format must be 'hepmc3', got {fmt!r}")

    def _validate_pythia(self):
        """Structural checks for the pythia backend. Pythia 8 is driven by a
        command ('.cmnd') file of `key = value` lines; we render one from a
        process preset plus raw `settings`, or take a verbatim `cmnd` file."""
        p = self.pythia
        proc = p.get("process", "dis")
        if proc not in PYTHIA_PROCESSES:
            raise ConfigError(
                f"unknown pythia.process {proc!r}; choose one of "
                f"{', '.join(PYTHIA_PROCESSES)} -- or drop pythia.process and give "
                f"raw pythia.settings / a verbatim pythia.cmnd file")
        settings = p.get("settings")
        if settings is not None and not isinstance(settings, list):
            raise ConfigError("pythia.settings must be a list of raw Pythia "
                              "'key = value' strings")
        if settings and any(not isinstance(s, str) for s in settings):
            raise ConfigError("pythia.settings entries must be strings, e.g. "
                              "'PhaseSpace:Q2Min = 1.0'")

    def _validate_beam_distributions(self):
        b = self.beam
        for group in (b.position_dist, b.direction_slopes):
            for name, d in (group or {}).items():
                _validate_dist(d, name)
        if b.momentum:
            _validate_dist(b.momentum, "momentum")
        if b.twiss:
            # Twiss defines position + angle + momentum together; independent
            # spreads on the same coordinates would be ambiguous.
            if b.position_dist or b.direction_slopes or b.momentum:
                raise ConfigError(
                    "beam.twiss is mutually exclusive with position_dist / "
                    "direction slopes / momentum (twiss defines them jointly)")
            for plane, tp in (("x", b.twiss.x), ("y", b.twiss.y)):
                if tp.beta <= 0:
                    raise ConfigError(f"twiss.{plane}.beta must be > 0")
                if tp.emittance < 0:
                    raise ConfigError(f"twiss.{plane}.emittance must be >= 0")


# --------------------------------------------------------------------------- #
# YAML front-end
# --------------------------------------------------------------------------- #
_TOP_KEYS = {"generator", "geometry", "beam", "projectile", "run",
             "geant4", "genie", "achilles", "pythia", "decay", "external", "mac"}


def _energy_from(raw) -> Energy:
    """Accept either a bare string (shorthand for mono at that value) or the
    full {mode, value, sigma, min, max, bins} mapping."""
    if raw is None:
        return Energy()
    if isinstance(raw, str):
        return Energy(mode="mono", value=raw)
    if not isinstance(raw, dict):
        raise ConfigError("beam.energy must be a string or a mapping")
    bins = []
    for b in raw.get("bins", []) or []:
        if not isinstance(b, dict) or "value" not in b:
            raise ConfigError("each energy.bins entry needs a 'value' (and optional 'weight')")
        bins.append({"value": str(b["value"]), "weight": float(b.get("weight", 1.0))})
    return Energy(
        mode=str(raw.get("mode", "mono")),
        value=str(raw.get("value", "1 GeV")),
        sigma=_opt_str(raw.get("sigma")),
        min=_opt_str(raw.get("min")),
        max=_opt_str(raw.get("max")),
        bins=bins,
    )


def _opt_str(v):
    return None if v is None else str(v)


def _distribution_from(raw) -> Distribution:
    """A scalar (str/number) -> fixed; a mapping {dist: gauss|uniform|fixed, ...}."""
    if raw is None:
        return Distribution(kind="fixed", value="0")
    if isinstance(raw, (str, int, float)):
        return Distribution(kind="fixed", value=str(raw))
    if not isinstance(raw, dict):
        raise ConfigError("a distribution must be a scalar or a mapping")
    kind = str(raw.get("dist", "fixed"))
    return Distribution(
        kind=kind,
        value=_opt_str(raw.get("value")),
        mean=_opt_str(raw.get("mean")),
        sigma=_opt_str(raw.get("sigma")),
        min=_opt_str(raw.get("min")),
        max=_opt_str(raw.get("max")),
    )


def _position_from(raw):
    """Return (position_str, position_dist). A string -> fixed; a {x,y,z} mapping
    -> per-axis Distributions (each axis a scalar or a distribution mapping)."""
    if raw is None:
        return "0 0 -20 cm", None
    if isinstance(raw, str):
        return raw, None
    if isinstance(raw, dict):
        dist = {axis: _distribution_from(raw.get(axis)) for axis in ("x", "y", "z")}
        return "0 0 0 mm", dist
    raise ConfigError("beam.position must be a string or an {x,y,z} mapping")


def _direction_from(raw):
    """Return (direction_str, angle_sigma, direction_slopes).
       - string           -> fixed direction
       - {theta_sigma}    -> fixed direction (optional 'central') + gaussian cone
       - {xprime,yprime}  -> angular slope distributions (requires host sampling)"""
    if raw is None:
        return "0 0 1", None, None
    if isinstance(raw, str):
        return raw, None, None
    if not isinstance(raw, dict):
        raise ConfigError("beam.direction must be a string or a mapping")
    central = str(raw.get("central", "0 0 1"))
    if "xprime" in raw or "yprime" in raw:
        slopes = {"xprime": _distribution_from(raw.get("xprime", 0)),
                  "yprime": _distribution_from(raw.get("yprime", 0))}
        return central, None, slopes
    return central, _opt_str(raw.get("theta_sigma")), None


def _momentum_from(raw):
    if raw is None:
        return None
    return _distribution_from(raw)


def _twiss_plane(raw, name) -> TwissPlane:
    if not isinstance(raw, dict):
        raise ConfigError(f"twiss.{name} must be a mapping with alpha, beta, emittance")
    try:
        return TwissPlane(alpha=float(raw.get("alpha", 0.0)),
                          beta=float(raw["beta"]),
                          emittance=float(raw["emittance"]))
    except (KeyError, TypeError, ValueError):
        raise ConfigError(f"twiss.{name} needs numeric beta and emittance (and optional alpha)")


def _twiss_from(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("beam.twiss must be a mapping")
    ref = raw.get("reference") or {}
    return Twiss(
        x=_twiss_plane(raw.get("x"), "x"),
        y=_twiss_plane(raw.get("y"), "y"),
        p0=str(raw.get("p0", "1 GeV/c")),
        dp_over_p=float(raw.get("dp_over_p", 0.0)),
        ref_position=str(ref.get("position", "0 0 0 mm")),
        ref_direction=str(ref.get("direction", "0 0 1")),
    )


def _as_pdg(v):
    """Return an int PDG code if v is an int or an all-digit (optionally signed)
    string, else None (it's a particle name like 'mu-')."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.lstrip("-").isdigit():
        return int(v)
    return None


def _particle_from(beam_raw):
    """Resolve (name_or_display_str, pdg). An explicit `pdg:` wins; otherwise a
    numeric `particle:` is treated as a PDG id; otherwise it is a name."""
    raw = beam_raw.get("particle", "e-")
    explicit = beam_raw.get("pdg")
    pdg = _as_pdg(explicit) if explicit is not None else _as_pdg(raw)
    if pdg is not None:
        return str(pdg), pdg
    return str(raw), None

# JTR: method of reading flux files
def _flux_from(raw) -> Flux:
    if not raw:
        return Flux()
    # If the user just wrote `flux: filename.root`
    if isinstance(raw, str):
        return Flux(file=raw)
    # If the user wrote the full dictionary block
    if isinstance(raw, dict):
        return Flux(
            file=raw.get("file"),
            mode=raw.get("mode", "gsimple")
        )
    return Flux()


def _beam_from(beam_raw: dict) -> Beam:
    pos_str, pos_dist = _position_from(beam_raw.get("position"))
    dir_str, cone, slopes = _direction_from(beam_raw.get("direction"))
    # a top-level angle_sigma still works and overrides a direction-mapping cone
    angle_sigma = _opt_str(beam_raw.get("angle_sigma")) or cone
    particle, pdg = _particle_from(beam_raw)
    return Beam(
        particle=particle,
        pdg=pdg,
        mass=_opt_str(beam_raw.get("mass")),
        energy=_energy_from(beam_raw.get("energy")),
        flux=_flux_from(beam_raw.get("flux")), # JTR: Flux file
        position=pos_str,
        direction=dir_str,
        angle_sigma=angle_sigma,
        position_dist=pos_dist,
        direction_slopes=slopes,
        momentum=_momentum_from(beam_raw.get("momentum")),
        twiss=_twiss_from(beam_raw.get("twiss")),
    )


def from_dict(data: dict) -> RunConfig:
    """Build a RunConfig from a parsed YAML/JSON mapping (no flag merge)."""
    if not isinstance(data, dict):
        raise ConfigError("config file must contain a top-level mapping")
    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise ConfigError(f"unknown top-level key(s): {', '.join(sorted(unknown))}")

    geometry = data.get("geometry") or {}
    if isinstance(geometry, str):           # tolerate `geometry: my.gdml` shorthand
        geometry = {"gdml": geometry}
    beam_raw = data.get("beam", data.get("projectile")) or {}

    cfg = RunConfig(
        generator=str(data.get("generator", "geant4")),
        gdml=_opt_str(geometry.get("gdml")),
        mac=_opt_str(data.get("mac")),
        beam=_beam_from(beam_raw),
        run=_run_from(data.get("run") or {}),
        geant4=dict(data.get("geant4") or {}),
        genie=dict(data.get("genie") or {}),
        pythia=dict(data.get("pythia") or {}),
        achilles=dict(data.get("achilles") or {}),
        decay=dict(data.get("decay") or {}),
        external=dict(data.get("external") or {}),
    )
    # YAML 1.1 parses `on`/`off` as booleans, so `neutrino_mode: off` arrives as
    # Python False; map it back to the auto/on/off vocabulary g4sim expects.
    nm = cfg.geant4.get("neutrino_mode")
    if isinstance(nm, bool):
        cfg.geant4["neutrino_mode"] = "on" if nm else "off"
    return cfg


def _run_from(raw: dict) -> RunSettings:
    seed = raw.get("seed")
    return RunSettings(
        events=int(raw.get("events", 100)),
        output=str(raw.get("output", "output.root")),
        seed=None if seed is None else int(seed),
    )


def from_yaml(path) -> RunConfig:
    import yaml  # lazy: only the --config path needs PyYAML
    text = Path(path).read_text()
    data = yaml.safe_load(text)
    if data is None:
        raise ConfigError(f"config file is empty: {path}")
    return from_dict(data)


# --------------------------------------------------------------------------- #
# Flag front-end + merge
# --------------------------------------------------------------------------- #
# Maps argparse dest -> where it lands in the config. Only these run-subcommand
# flags participate in config (image/local/outdir/display/dry_run are
# orchestration, handled separately by the caller).
_FLAG_FIELDS = ("generator", "gdml", "mac", "particle", "energy",
                "position", "direction", "n", "neutrino_mode", "field")


def _apply_flag(cfg: RunConfig, name: str, value):
    if name == "generator":
        cfg.generator = value
    elif name == "gdml":
        cfg.gdml = value
    elif name == "mac":
        cfg.mac = value
    elif name == "particle":
        # a numeric --particle is a PDG id (mirrors the YAML behavior)
        pdg = _as_pdg(value)
        cfg.beam.pdg = pdg
        cfg.beam.particle = str(pdg) if pdg is not None else value
    elif name == "energy":
        cfg.beam.energy.value = value  # flags only drive the mono/nominal value
    elif name == "position":
        cfg.beam.position = value
    elif name == "direction":
        cfg.beam.direction = value
    elif name == "n":
        cfg.run.events = int(value)
    elif name == "neutrino_mode":
        cfg.geant4["neutrino_mode"] = value
    elif name == "field":
        cfg.geant4["field"] = value


def from_flags(args) -> RunConfig:
    """Build a RunConfig purely from CLI flags (the historical interface)."""
    cfg = RunConfig()
    for name in _FLAG_FIELDS:
        if hasattr(args, name):
            _apply_flag(cfg, name, getattr(args, name))
    return cfg


def load(args, defaults=None) -> RunConfig:
    """Resolve the final RunConfig for a `run` invocation.

    Without --config, the config is built entirely from flags (identical to the
    historical behavior). With --config, the YAML is the base and any flag whose
    value differs from its parser default is overlaid on top (flag > YAML >
    default). `defaults` maps flag dest -> parser default, used to detect which
    flags were explicitly given; when omitted, every present flag overrides.
    """
    cfg_path = getattr(args, "config", None)
    if not cfg_path:
        return from_flags(args).validate()

    cfg = from_yaml(cfg_path)
    defaults = defaults or {}
    for name in _FLAG_FIELDS:
        if not hasattr(args, name):
            continue
        value = getattr(args, name)
        # Only overlay flags the user actually set (value != parser default).
        if name in defaults and value == defaults[name]:
            continue
        if value is None:
            continue
        _apply_flag(cfg, name, value)
    return cfg.validate()
