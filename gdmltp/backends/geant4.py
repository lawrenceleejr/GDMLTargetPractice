"""Geant4 backend: render a Geant4 macro from the common RunConfig and run g4sim.

`build_macro` emits the full messenger surface g4sim exposes (energy modes,
seeds, angular spread, arbitrary spectra) -- not just the mono-energy subset the
old flag-only path produced. Every command here is one g4sim already understands
(see g4sim/PrimaryGeneratorMessenger.cc, DetectorMessenger.cc, RunActionMessenger.cc).
"""
import shutil
from pathlib import Path

from .base import Backend, Prepared, image_override

DEFAULT_IMAGE = "ghcr.io/lawrenceleejr/gdmltargetpractice:main"
GENERATED_MACRO = "gdmltp_run.mac"
BEAM_FILE = "beam.dat"


def _progress(events):
    # print progress often enough for a smooth host-side bar (~100 updates),
    # but never every-single-event on very large runs
    try:
        return max(1, int(events) // 100)
    except (TypeError, ValueError):
        return 100


_NEUTRINO_PDGS = {12, -12, 14, -14, 16, -16}
DEFAULT_NU_BIAS = 5.0e12          # matches the repo's hand-written neutrino macros


def _neutrino_bias_lines(cfg):
    """Geant4's built-in neutrino processes have cross sections so small that
    unbiased runs record essentially no interactions; g4sim's /gdmltp/neutrinoBias
    command enables them and scales the cross sections via the G4EmParameters C++
    API. (Earlier this emitted the /physics_lists/em/Nu* UI commands, but those
    are not registered in every Geant4 build -- and an unknown command aborts the
    batch regardless of /control/suppressAbortion -- so we drive the API through
    our own always-present command instead.)

    geant4.neutrino_bias: auto (default: on when the primary is a neutrino),
    on/off, or a mapping {enable, factor, cc_bias, nc_bias, nucleus_bias,
    detector_name}.
    """
    raw = cfg.geant4.get("neutrino_bias", "auto")
    if isinstance(raw, bool):                     # YAML 1.1: on/off arrive as bools
        raw = {"enable": "on" if raw else "off"}
    elif isinstance(raw, str):
        raw = {"enable": raw}
    elif not isinstance(raw, dict):
        raw = {"enable": "auto"}

    enable = raw.get("enable", "auto")
    if isinstance(enable, bool):
        enable = "on" if enable else "off"
    if enable == "off":
        return []
    if enable == "auto":
        pdg = cfg.beam.pdg_code()
        if pdg not in _NEUTRINO_PDGS:
            return []

    alias_names = ["TAUNUNUCBIASCC", "TAUNUNUCBIASNC", "TAUANUNUCBIASCC",
                   "TAUANUNUCBIASNC", "MUNUNUCBIASCC", "MUNUNUCBIASNC",
                   "MUANUNUCBIASCC", "MUANUNUCBIASNC", "ELNUNUCBIASCC",
                   "ELNUNUCBIASNC", "ELANUNUCBIASCC", "ELANUNUCBIASNC",
                   "NUELECCCBIAS", "NUELECNCBIAS"]
    bias_names = ["TauNuNucleusCcBias", "TauNuNucleusNcBias",
                  "TauANuNucleusCcBias", "TauANuNucleusNcBias", 
                  "MuNuNucleusCcBias", "MuNuNucleusNcBias",
                  "MuANuNucleusCcBias", "MuANuNucleusNcBias",
                  "ElNuNucleusCcBias", "ElNuNucleusNcBias","ElANuNucleusCcBias",
                  "ElANuNucleusNcBias", "NuElectronCcBias", "NuElectronNcBias"]

    factor = float(raw.get("factor", DEFAULT_NU_BIAS))
    cmd_prefix = f"/custom/biasing/"
    cmd_list = []
    for i in range(len(alias_names)):
        cmd_list.append(cmd_prefix + bias_names[i] + " " + \
            str(raw.get(alias_names[i], factor)))

    return cmd_list

def _exit_hepmc_lines(cfg):
    """`geant4.exit_hepmc` -> the /analysis/exit* commands (see g4sim/ExitWriter).

    Writes the particles LEAVING a volume as HepMC3 -- a scoring-plane /
    phase-space file. g4sim also READS HepMC3 (`/gun/hepmcFile`), so this is how
    one run feeds the next: stage 1 records what crosses the surface, stage 2
    replays it as its primaries. Everything but the filename is optional:

      exit_hepmc:   exit.hepmc     # enables the export
      exit_volume:  Detector       # default World = everything that escapes
      exit_min_ke:  "1 MeV"        # skip soft crossings (shower exits are huge)
      exit_kill:    true           # stop tracks at the surface (staged runs)
    """
    path = cfg.geant4.get("exit_hepmc")
    if not path:
        return []
    lines = [f"/analysis/exitHepMC {path}"]
    volume = cfg.geant4.get("exit_volume")
    if volume:
        lines.append(f"/analysis/exitVolume {volume}")
    min_ke = cfg.geant4.get("exit_min_ke")
    if min_ke:
        lines.append(f"/analysis/exitMinKE {min_ke}")
    if cfg.geant4.get("exit_kill"):
        lines.append("/analysis/exitKill true")
    return lines


def build_macro(cfg, beam_file=None) -> str:
    """Render `cfg` to Geant4 macro text.

    Order matters only in that /analysis/neutrinoMode, the seed and the field
    must precede /run/beamOn (branches/field are set up at run start); the gun
    commands are state-flexible. We keep the historical layout:
    readGDML -> initialize -> neutrinoMode -> [seed] -> [field] -> gun -> beamOn.

    When `beam_file` is given (host-sampled distributions / Twiss), the per-event
    gun block is replaced by a single `/gun/beamFile`, and g4sim replays one
    sampled primary per event.
    """
    beam = cfg.beam
    e = beam.energy
    gdml_name = Path(cfg.gdml).name if cfg.gdml else ""
    nmode = cfg.geant4.get("neutrino_mode", "auto")

    lines = [f"/detector/readGDML {gdml_name}"]
    lines += [
        "/run/initialize",
        f"/analysis/neutrinoMode {nmode}",
    ]
    lines += _neutrino_bias_lines(cfg)       # must follow /run/initialize
    lines += _exit_hepmc_lines(cfg)          # must precede /run/beamOn
    if cfg.run.seed is not None:
        lines.append(f"/random/setSeeds {int(cfg.run.seed)} {int(cfg.run.seed) + 1}")
    field = cfg.geant4.get("field")
    if field:
        lines.append(f"/detector/setGlobalField {field}")

    if beam_file:
        lines.append(f"/gun/beamFile {beam_file}")
        lines.append(f"/run/printProgress {_progress(cfg.run.events)}")
        lines.append(f"/run/beamOn {int(cfg.run.events)}")
        return "\n".join(lines) + "\n"

    if beam.is_pdg():
        lines.append(f"/gun/particlePDG {beam.pdg}")
    else:
        lines.append(f"/gun/particle {beam.particle}")
    lines.append(f"/gun/energyMode {e.mode}")
    if e.mode == "gauss" and e.sigma:
        lines.append(f"/gun/gaussSigma {e.sigma}")
    if e.mode == "exp":
        if e.min:
            lines.append(f"/gun/energyMin {e.min}")
        if e.max:
            lines.append(f"/gun/energyMax {e.max}")
    if e.mode == "arb":
        lines.append("/gun/clearEnergyBins")
        for b in e.bins:
            lines.append(f"/gun/addEnergyBin {b['value']} {b['weight']}")
    else:
        lines.append(f"/gun/energy {e.value}")

    if beam.angle_sigma:
        lines.append(f"/gun/angleSigma {beam.angle_sigma}")
    lines.append(f"/gun/position {beam.position}")
    lines.append(f"/gun/direction {beam.direction}")
    lines.append(f"/run/printProgress {_progress(cfg.run.events)}")
    lines.append(f"/run/beamOn {int(cfg.run.events)}")
    return "\n".join(lines) + "\n"


class Geant4Backend(Backend):
    name = "geant4"
    default_image = DEFAULT_IMAGE

    def image_for(self, cfg) -> str:
        img = image_override(self.name) or self.default_image
        if cfg.geant4.get("celeritas"):
            # tag variant: ...:main -> ...:main-celeritas
            if ":" in img:
                repo, tag = img.rsplit(":", 1)
                return f"{repo}:{tag}-celeritas"
            return f"{img}:latest-celeritas"
        return img

    def celer_disable(self, cfg) -> bool:
        # The Celeritas EM offload assumes zero field; disable it when a field
        # is set (mirrors the historical run.py behavior).
        return bool(cfg.geant4.get("field"))

    def prepare(self, cfg, outdir, image=None) -> Prepared:
        outdir = Path(outdir)
        if cfg.mac:
            macro_name = Path(cfg.mac).name
            src = Path(cfg.mac)
            if src.resolve().parent != outdir.resolve() and src.exists():
                shutil.copy(src, outdir / macro_name)
        elif cfg.beam.needs_sampling():
            from .. import beam as beammod
            s = beammod.sample(cfg, int(cfg.run.events), seed=cfg.run.seed)
            beammod.write_beam_file(s, outdir / BEAM_FILE)
            macro_name = GENERATED_MACRO
            (outdir / macro_name).write_text(build_macro(cfg, beam_file=BEAM_FILE))
        else:
            macro_name = GENERATED_MACRO
            (outdir / macro_name).write_text(build_macro(cfg))

        env = {}
        if self.celer_disable(cfg):
            env["CELER_DISABLE"] = "1"

        return Prepared(argv=[macro_name],
                        image=image or self.image_for(cfg),
                        env=env, output="output.root")
