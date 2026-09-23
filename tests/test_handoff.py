"""Generator -> Geant4 transport hand-off: event file, macro, nu-block merge,
and the two-stage orchestration (subprocess mocked; no Docker/engines)."""
import sys

import numpy as np
import pytest

from gdmltp import io, config, handoff, run
from gdmltp.backends import genie_convert


@pytest.fixture()
def vertex_root(synth_gst, tmp_path):
    """A vertex-level ntuple as the genie backend would produce (25 events)."""
    out = str(tmp_path / "vertex.root")
    genie_convert.convert(synth_gst, out)
    return out


# --- event file --------------------------------------------------------------
def test_write_event_file_roundtrip(vertex_root, tmp_path):
    path = tmp_path / "events.hepmc"
    n = handoff.write_event_file(vertex_root, path)
    assert n == 25
    events = handoff.read_event_file(path)
    assert len(events) == 25
    ev0 = io.load_events(vertex_root, entry_start=0, entry_stop=1)[0]
    vtx, parts = events[0]
    assert vtx[0] == pytest.approx(float(ev0.nu["nu_vertexX"]), rel=1e-5)
    assert len(parts) == int(ev0.scalars["nTracks"])
    assert parts[0][0] == int(ev0.trk["trk_pdg"][0])
    assert parts[0][1][2] == pytest.approx(float(ev0.trk["trk_pz"][0]), rel=1e-5)


def test_transport_macro():
    mac = handoff.build_transport_macro("lar.gdml", 25, seed=3, field="0 0 2 tesla")
    assert "/detector/readGDML lar.gdml" in mac
    assert "/gun/hepmcFile events.hepmc" in mac
    assert "/run/beamOn 25" in mac
    assert "/analysis/neutrinoMode off" in mac       # nu block comes from the merge
    assert "/random/setSeeds 3 4" in mac
    assert "/detector/setGlobalField 0 0 2 tesla" in mac


# --- merge -------------------------------------------------------------------
def test_merge_nu_block(vertex_root, tmp_path):
    sys.path.insert(0, str(tmp_path.parent))
    from conftest import write_synthetic
    transported = write_synthetic(tmp_path / "transported.root", n_events=25, seed=5)
    out = str(tmp_path / "merged.root")
    handoff.merge_nu_block(transported, vertex_root, out)

    brs = set(io.available_branches(out))
    for b in io.SCALAR_BRANCHES + io.TRK_BRANCHES + io.STEP_BRANCHES + io.NU_BRANCHES:
        assert b in brs, f"merged file missing {b}"

    import uproot
    t = uproot.open(out)["tree"]
    tv = uproot.open(vertex_root)["tree"]
    tt = uproot.open(transported)["tree"]
    # nu block + primary identity from the generator
    assert np.array_equal(t["nu_Q2"].array(library="np"), tv["nu_Q2"].array(library="np"))
    assert np.all(t["primaryPDG"].array(library="np") == 14)
    # transport record intact
    assert np.array_equal(t["totalEdep"].array(library="np"),
                          tt["totalEdep"].array(library="np"))
    assert t["nSteps"].array(library="np").sum() > 0


def test_merge_event_count_mismatch(vertex_root, tmp_path):
    sys.path.insert(0, str(tmp_path.parent))
    from conftest import write_synthetic
    transported = write_synthetic(tmp_path / "t.root", n_events=10, seed=5)
    with pytest.raises(ValueError, match="25"):
        handoff.merge_nu_block(transported, vertex_root, str(tmp_path / "m.root"))


# --- two-stage orchestration ---------------------------------------------------
def test_run_config_transport_two_stages(repo_root, synth_gst, tmp_path, monkeypatch):
    """genie.transport runs the generator image, then the geant4 image with the
    transport macro, and merges -- verified with docker mocked."""
    sys.path.insert(0, str(tmp_path.parent))
    from conftest import write_synthetic
    calls = []

    def fake_popen(cmd, **kw):
        calls.append(cmd)
        if len(calls) == 1:           # generator stage: emit vertex-level output
            genie_convert.convert(synth_gst, tmp_path / "output.root")
        else:                          # transport stage: emit transported output
            write_synthetic(tmp_path / "output.root", n_events=25, seed=6)

        class P:  # noqa
            stdout = iter(["--> Event 0 starts."])
            returncode = 0
            def wait(self):  # noqa
                return 0
        return P()

    monkeypatch.setattr(run.subprocess, "Popen", fake_popen)
    cfg = config.RunConfig(
        generator="genie",
        gdml=str(repo_root / "gdml" / "liquid_argon_1m3.gdml"),
        beam=config.Beam(particle="nu_mu",
                         energy=config.Energy(mode="mono", value="2 GeV")),
        run=config.RunSettings(events=25),
        genie={"tune": "G18_10a_00_000", "transport": True})
    run.run_config(cfg, outdir=str(tmp_path))

    assert len(calls) == 2
    assert any("gdmltargetpractice-genie" in c for c in calls[0])
    assert any("gdmltargetpractice:main" in c for c in calls[1])   # geant4 image
    assert calls[1][-1] == handoff.TRANSPORT_MACRO
    assert (tmp_path / handoff.EVENT_FILE).exists()

    import uproot
    t = uproot.open(tmp_path / "output.root")["tree"]
    assert np.all(t["primaryPDG"].array(library="np") == 14)     # nu from generator
    assert t["nSteps"].array(library="np").sum() > 0             # steps from transport


def test_run_config_transport_dry_run(repo_root, tmp_path, capsys):
    cfg = config.RunConfig(
        generator="genie",
        gdml=str(repo_root / "gdml" / "liquid_argon_1m3.gdml"),
        beam=config.Beam(particle="nu_mu",
                         energy=config.Energy(mode="mono", value="2 GeV")),
        run=config.RunSettings(events=5),
        genie={"transport": True})
    run.run_config(cfg, outdir=str(tmp_path), dry_run=True)
    out = capsys.readouterr().out
    assert "dry-run" in out and "transport" in out


# --- stage packaging: every generator path ends in Geant4 --------------------
def _genie_cfg(repo_root, **genie):
    return config.RunConfig(
        generator="genie",
        gdml=str(repo_root / "gdml" / "liquid_argon_1m3.gdml"),
        beam=config.Beam(particle="nu_mu",
                         energy=config.Energy(mode="mono", value="2 GeV")),
        run=config.RunSettings(events=25),
        genie={"tune": "G18_10a_00_000", **genie})


@pytest.mark.parametrize("generator", config.VERTEX_LEVEL_GENERATORS)
def test_transport_is_the_default_for_every_vertex_level_generator(generator):
    """No `transport:` key means Geant4 still transports: the common ntuple is
    supposed to carry a transport record whatever produced the interaction."""
    cfg = config.RunConfig(generator=generator, gdml="x.gdml")
    assert run._wants_transport(cfg) is True
    setattr(cfg, generator, {"transport": False})
    assert run._wants_transport(cfg) is False


def test_stage_inputs_packages_a_self_contained_stage2(vertex_root, tmp_path):
    """Stage 1 leaves a complete stage-2 job in the run directory: the vertex
    record, the HepMC3 interchange, the macro, and the spec tying them
    together -- so a Geant4 image can finish the run on its own."""
    import shutil
    shutil.copy(vertex_root, tmp_path / "output.root")
    (tmp_path / "lar.gdml").write_text("<gdml/>")
    spec = handoff.stage_inputs(tmp_path, "lar.gdml", seed=7, generator="genie")

    assert spec["events"] == 25 and spec["generator"] == "genie"
    for name in (handoff.VERTEX_FILE, handoff.EVENT_FILE,
                 handoff.TRANSPORT_MACRO, handoff.STAGE_SPEC):
        assert (tmp_path / name).exists(), name
    assert not (tmp_path / "output.root").exists()   # incomplete until Geant4 ran
    mac = (tmp_path / handoff.TRANSPORT_MACRO).read_text()
    assert f"/gun/hepmcFile {handoff.EVENT_FILE}" in mac and "/run/beamOn 25" in mac
    assert handoff.read_spec(tmp_path) == spec


def test_read_spec_without_a_generator_stage_explains_itself(tmp_path):
    with pytest.raises(FileNotFoundError, match="nothing to transport"):
        handoff.read_spec(tmp_path)


def test_transport_command_finishes_a_generator_stage(repo_root, synth_gst,
                                                     tmp_path, monkeypatch):
    """`gdmltp transport -o rundir` (what the Geant4 image runs when a bare
    `docker run` of a generator image cannot chain containers) replays the
    HepMC3 file and merges the generator record in."""
    sys.path.insert(0, str(tmp_path.parent))
    from conftest import write_synthetic
    genie_convert.convert(synth_gst, tmp_path / "output.root")
    (tmp_path / "lar.gdml").write_text("<gdml/>")
    handoff.stage_inputs(tmp_path, "lar.gdml", generator="genie")

    calls = []

    def fake_popen(cmd, **kw):
        calls.append(cmd)
        write_synthetic(tmp_path / "output.root", n_events=25, seed=6)

        class P:
            stdout = iter(["--> Event 0 starts."])
            returncode = 0
            def wait(self):  # noqa
                return 0
        return P()

    monkeypatch.setattr(run.subprocess, "Popen", fake_popen)
    assert run.transport(outdir=str(tmp_path), image="g4:test") == 0
    assert calls[0][-1] == handoff.TRANSPORT_MACRO
    assert "g4:test" in calls[0]

    import uproot
    t = uproot.open(tmp_path / "output.root")["tree"]
    assert np.all(t["primaryPDG"].array(library="np") == 14)    # generator record
    assert t["nSteps"].array(library="np").sum() > 0            # Geant4 transport


def test_generator_image_without_geant4_says_what_to_run(repo_root, synth_gst,
                                                        tmp_path, monkeypatch):
    """Inside a generator image there is no g4sim, and a bare `docker run`
    cannot start the Geant4 container: the stage-1 products must be kept and the
    error must name the command that finishes the run."""
    def fake_popen(cmd, **kw):
        genie_convert.convert(synth_gst, tmp_path / "output.root")

        class P:
            stdout = iter([""])
            returncode = 0
            def wait(self):  # noqa
                return 0
        return P()

    monkeypatch.setattr(run.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(run, "_geant4_engine_here", lambda: False)
    with pytest.raises(RuntimeError, match="transport -o"):
        run.run_config(_genie_cfg(repo_root), outdir=str(tmp_path), local=True)
    # nothing is lost: stage 2 can be run from these
    for name in (handoff.VERTEX_FILE, handoff.EVENT_FILE, handoff.STAGE_SPEC):
        assert (tmp_path / name).exists(), name
    assert not (tmp_path / "output.root").exists()


def test_spec_records_the_engine_image_for_a_later_transport(vertex_root, tmp_path,
                                                            monkeypatch):
    """A celeritas (or pinned) Geant4 image chosen at generation time must be
    the one a standalone `gdmltp transport` uses later."""
    import shutil
    shutil.copy(vertex_root, tmp_path / "output.root")
    (tmp_path / "lar.gdml").write_text("<gdml/>")
    handoff.stage_inputs(tmp_path, "lar.gdml", image="g4:celeritas")

    calls = []
    monkeypatch.setattr(run, "_exec_stage",
                        lambda argv, image, *a, **k: calls.append(image))
    monkeypatch.setattr(run.handoff, "merge_nu_block", lambda *a, **k: None)
    run.transport(outdir=str(tmp_path))
    assert calls == ["g4:celeritas"]

    calls.clear()
    monkeypatch.setenv("GDMLTP_IMAGE_GEANT4", "g4:pinned")   # env still wins
    run.transport(outdir=str(tmp_path))
    assert calls == ["g4:pinned"]


def test_stage_generator_on_a_single_stage_backend_explains_itself(repo_root, tmp_path):
    """--stage generator only makes sense for a two-engine run."""
    cfg = config.RunConfig(
        gdml=str(repo_root / "gdml" / "bpe_slab.gdml"),
        beam=config.Beam(particle="proton",
                         energy=config.Energy(mode="mono", value="1 GeV")),
        run=config.RunSettings(events=1))
    with pytest.raises(config.ConfigError, match="single Geant4 stage"):
        run.run_config(cfg, outdir=str(tmp_path), dry_run=True, stage="generator")

    nu = _genie_cfg(repo_root, transport=False)
    with pytest.raises(config.ConfigError, match="transport: false"):
        run.run_config(nu, outdir=str(tmp_path), dry_run=True, stage="generator")
