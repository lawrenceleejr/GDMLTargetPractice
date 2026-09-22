"""Backend registry + Geant4Backend macro rendering and run plan."""
import pytest

from gdmltp import config, backends
from gdmltp.backends import geant4


def _cfg(**geant4_block):
    return config.RunConfig(gdml="gdml/water_phantom_30cm.gdml", geant4=geant4_block)


# --- registry -------------------------------------------------------------- #
def test_registry_returns_geant4():
    b = backends.get("geant4")
    assert b.name == "geant4"


def test_registry_unknown_raises():
    with pytest.raises(ValueError, match="unknown generator"):
        backends.get("fluka")


def test_default_image_and_celeritas_variant():
    b = backends.get("geant4")
    assert b.image_for(_cfg()) == geant4.DEFAULT_IMAGE
    assert b.image_for(_cfg(celeritas=True)).endswith("-celeritas")


def test_image_env_override(monkeypatch):
    """GDMLTP_IMAGE_<BACKEND> pins the image for EVERY stage (the transport
    stage of a two-stage run has no --image of its own) -- what CI uses to test
    a freshly built image pair."""
    b = backends.get("geant4")
    monkeypatch.setenv("GDMLTP_IMAGE_GEANT4", "ghcr.io/x/g4:pr-1")
    assert b.image_for(_cfg()) == "ghcr.io/x/g4:pr-1"
    assert b.image_for(_cfg(celeritas=True)) == "ghcr.io/x/g4:pr-1-celeritas"
    monkeypatch.setenv("GDMLTP_IMAGE_GEANT4", "   ")          # blank = unset
    assert b.image_for(_cfg()) == geant4.DEFAULT_IMAGE
    monkeypatch.setenv("GDMLTP_IMAGE_GENIE", "ghcr.io/x/genie:pr-1")
    assert backends.get("genie").image_for(_cfg()) == "ghcr.io/x/genie:pr-1"


# --- macro rendering ------------------------------------------------------- #
def test_macro_mono_basics():
    mac = geant4.build_macro(config.RunConfig(
        gdml="water.gdml",
        beam=config.Beam(particle="proton",
                         energy=config.Energy(mode="mono", value="150 MeV")),
        run=config.RunSettings(events=500, seed=42)))
    assert "/detector/readGDML water.gdml" in mac
    assert "/gun/energyMode mono" in mac
    assert "/gun/energy 150 MeV" in mac
    assert "/random/setSeeds 42 43" in mac
    assert "/run/beamOn 500" in mac


def test_macro_particle_by_pdg():
    mac = geant4.build_macro(config.RunConfig(
        gdml="g.gdml", beam=config.Beam(particle="2212", pdg=2212)))
    assert "/gun/particlePDG 2212" in mac
    assert "/gun/particle " not in mac        # name form not emitted


def test_macro_particle_by_name_unchanged():
    mac = geant4.build_macro(config.RunConfig(
        gdml="g.gdml", beam=config.Beam(particle="mu-")))
    assert "/gun/particle mu-" in mac
    assert "/gun/particlePDG" not in mac


def test_macro_gauss_emits_sigma():
    mac = geant4.build_macro(config.RunConfig(
        gdml="g.gdml",
        beam=config.Beam(energy=config.Energy(mode="gauss", value="3 GeV", sigma="500 MeV"))))
    assert "/gun/energyMode gauss" in mac
    assert "/gun/gaussSigma 500 MeV" in mac


def test_macro_exp_emits_range():
    mac = geant4.build_macro(config.RunConfig(
        gdml="g.gdml",
        beam=config.Beam(energy=config.Energy(mode="exp", value="2 GeV",
                                              min="200 MeV", max="20 GeV"))))
    assert "/gun/energyMin 200 MeV" in mac
    assert "/gun/energyMax 20 GeV" in mac


def test_macro_arb_emits_bins():
    mac = geant4.build_macro(config.RunConfig(
        gdml="g.gdml",
        beam=config.Beam(energy=config.Energy(mode="arb", bins=[
            {"value": "500 MeV", "weight": 2.0}, {"value": "1 GeV", "weight": 1.0}]))))
    assert "/gun/clearEnergyBins" in mac
    assert "/gun/addEnergyBin 500 MeV 2.0" in mac
    assert "/gun/addEnergyBin 1 GeV 1.0" in mac
    assert "/gun/energy " not in mac          # arb does not set a single energy


def test_macro_neutrino_bias_auto():
    """A neutrino primary auto-enables biasing before /run/initialize via our own
    custom biasing commands (unbiased Geant4 neutrino runs record almost no
    interactions), driving the G4EmParameters API rather than the /physics_lists/
    em/Nu* UI commands that some Geant4 builds don't register."""
    mac = geant4.build_macro(config.RunConfig(
        gdml="g.gdml", beam=config.Beam(particle="nu_mu")))
    # JTR: Updating these tests for the new neutrino biasing
    assert "/custom/biasing/MuNuNucleusCcBias" in mac
    assert "/custom/biasing/MuNuNucleusNcBias" in mac
    # The auto-bias value is 5e12, which the backend formats as a large integer/float
    assert "5000000000000.0" in mac


def test_macro_neutrino_bias_off_and_custom():
    off = geant4.build_macro(config.RunConfig(
        gdml="g.gdml", beam=config.Beam(particle="nu_mu"),
        geant4={"neutrino_bias": "off"}))
    
    # JTR: Ensure no biasing commands are generated when turned off
    assert "/custom/biasing/MuNuNucleusCcBias" not in off

    custom = geant4.build_macro(config.RunConfig(
        gdml="g.gdml", beam=config.Beam(particle="nu_e"),
        geant4={"neutrino_bias": {"factor": 1e10, "ELNUNUCBIASNC": 3e9}}))

    assert "/custom/biasing/ElNuNucleusCcBias" in custom
    assert "/custom/biasing/ElNuNucleusCcBias 10000000000.0" in custom  # Factor 1e10 applied to CC
    assert "/custom/biasing/ElNuNucleusNcBias 3000000000.0" in custom   # Custom 3e9 applied to NC


def test_macro_no_bias_for_charged_primaries():
    mac = geant4.build_macro(config.RunConfig(
        gdml="g.gdml", beam=config.Beam(particle="proton")))
    assert "/physics_lists" not in mac


def test_macro_field_and_angle_sigma():
    mac = geant4.build_macro(config.RunConfig(
        gdml="g.gdml",
        beam=config.Beam(angle_sigma="10 deg"),
        geant4={"field": "0 0 5 tesla"}))
    assert "/detector/setGlobalField 0 0 5 tesla" in mac
    assert "/gun/angleSigma 10 deg" in mac
    # field set after initialize, before the gun block
    assert mac.index("/run/initialize") < mac.index("setGlobalField") < mac.index("/gun/particle")


# --- prepare (run plan) ---------------------------------------------------- #
def test_prepare_generates_macro(tmp_path):
    b = backends.get("geant4")
    prep = b.prepare(_cfg(), tmp_path)
    assert prep.argv == [geant4.GENERATED_MACRO]
    assert (tmp_path / geant4.GENERATED_MACRO).exists()
    assert prep.env == {}                       # no field -> no CELER_DISABLE
    assert prep.image == geant4.DEFAULT_IMAGE


def test_prepare_field_sets_celer_disable(tmp_path):
    b = backends.get("geant4")
    prep = b.prepare(_cfg(field="0 0 5 tesla"), tmp_path)
    assert prep.env.get("CELER_DISABLE") == "1"


def test_prepare_sampled_beam_writes_beamfile(tmp_path):
    """A distribution beam makes the geant4 macro use /gun/beamFile + writes beam.dat."""
    b = backends.get("geant4")
    cfg = config.RunConfig(
        gdml="gdml/water_phantom_30cm.gdml",
        beam=config.Beam(particle="mu-",
                         momentum=config.Distribution(kind="fixed", value="500 MeV/c"),
                         position_dist={"x": config.Distribution(kind="gauss", sigma="2 mm"),
                                        "y": config.Distribution(kind="fixed", value="0"),
                                        "z": config.Distribution(kind="fixed", value="-200 mm")}),
        run=config.RunSettings(events=25, seed=1))
    cfg.validate()
    prep = b.prepare(cfg, tmp_path)
    assert (tmp_path / geant4.BEAM_FILE).exists()
    macro = (tmp_path / prep.argv[0]).read_text()
    assert "/gun/beamFile beam.dat" in macro
    assert "/run/beamOn 25" in macro
    # 25 sampled primaries in the beam file
    from gdmltp import beam as beammod
    assert len(beammod.read_beam_file(tmp_path / geant4.BEAM_FILE)) == 25


def test_prepare_verbatim_mac_is_copied(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    mac = src / "my.mac"
    mac.write_text("/run/beamOn 1\n")
    out = tmp_path / "out"
    out.mkdir()
    cfg = config.RunConfig(gdml=None, mac=str(mac))
    prep = backends.get("geant4").prepare(cfg, out)
    assert prep.argv == ["my.mac"]
    assert (out / "my.mac").exists()


def test_xray_example_energy_is_the_0p1nm_wavelength(repo_root):
    """examples/tissue_lead_xray.yaml quotes its beam as a wavelength (0.1 nm)
    and its energy as the photon energy that follows, E = hc/lambda. Keep the
    two in step: a 12.4 keV gamma is the whole point of the example."""
    from gdmltp.beam import _ene_mev

    cfg = config.from_yaml(str(repo_root / "examples" / "tissue_lead_xray.yaml")).validate()
    assert cfg.beam.particle == "gamma"
    assert cfg.gdml == "gdml/tissue_phantom_lead_1mm.gdml"

    hc_eV_nm = 1239.841984                       # CODATA h*c, eV.nm
    expected_MeV = hc_eV_nm / 0.1 * 1e-6         # lambda = 0.1 nm
    assert _ene_mev(cfg.beam.energy.value) == pytest.approx(expected_MeV, rel=1e-4)

    mac = geant4.build_macro(cfg)
    assert "/detector/readGDML tissue_phantom_lead_1mm.gdml" in mac
    assert "/gun/particle gamma" in mac
    assert f"/gun/energy {cfg.beam.energy.value}" in mac
