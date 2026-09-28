"""The in-container GENIE driver (genie/run_genie.py) builds the right gevgen/
gntpc commands. subprocess and the converter are mocked, so no GENIE needed."""
import importlib.util
import json
from pathlib import Path

import pytest


def _load_driver(repo_root):
    path = repo_root / "genie" / "run_genie.py"
    spec = importlib.util.spec_from_file_location("run_genie", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_job(tmp_path, **over):
    job = {
        "generator": "genie", "gdml": "liquid_argon_1m3.gdml",
        "probe": 14, "target": 1000180400,
        "flux": {"mode": "mono", "value": "2 GeV", "min": None, "max": None, "bins": []},
        "position": "0 0 -50 cm", "direction": "0 0 1",
        "events": 100, "output": "output.root",
        "tune": "G18_10a_00_000", "cross_sections": "auto",
        "event_generator_list": "Default", "length_units": "cm", "seed": 42,
    }
    job.update(over)
    p = tmp_path / "genie_job.json"
    p.write_text(json.dumps(job))
    return p


def _bake_sf_tables(sf_root, tune):
    """Minimal stand-in for baked HEDIS structure-function tables. GENIE reads
    them from <HEDIS_SF_DATA_PATH>/<tune>/, one level below the base dir."""
    d = sf_root / tune
    d.mkdir(parents=True)
    (d / "QrkSF_LO_nu_cc_p_iq1_fq2.dat").write_text("")
    (d / "NucSF_NLO_nu_cc_p.dat").write_text("")
    return d


def test_driver_builds_gevgen_and_converts(repo_root, tmp_path, monkeypatch):
    mod = _load_driver(repo_root)
    monkeypatch.setenv("GENIE_XSEC_FILE", "/opt/splines.xml")   # skip on-demand gmkspl
    calls = []
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or type("R", (), {"returncode": 0})())

    converted = {}
    import gdmltp.backends.genie_convert as gc
    monkeypatch.setattr(gc, "convert",
                        lambda gst, out, **kw: converted.update(gst=gst, out=out, kw=kw))

    job = _write_job(tmp_path)
    assert mod.run(str(job)) == 0

    gevgen = calls[0]
    assert gevgen[0] == "gevgen"
    assert "-p" in gevgen and gevgen[gevgen.index("-p") + 1] == "14"
    assert "-t" in gevgen and gevgen[gevgen.index("-t") + 1] == "1000180400"
    assert "-n" in gevgen and gevgen[gevgen.index("-n") + 1] == "100"
    assert "--tune" in gevgen and gevgen[gevgen.index("--tune") + 1] == "G18_10a_00_000"
    assert "-e" in gevgen and gevgen[gevgen.index("-e") + 1] == "2"     # 2 GeV mono
    assert "--seed" in gevgen and gevgen[gevgen.index("--seed") + 1] == "42"

    gntpc = calls[1]
    assert gntpc[0] == "gntpc" and "gst" in gntpc

    # conversion invoked with the geometry's length units
    assert converted["kw"].get("vtx_units") == "cm"
    assert converted["out"].endswith("output.root")


def test_driver_exp_flux(repo_root, tmp_path, monkeypatch):
    mod = _load_driver(repo_root)
    monkeypatch.setenv("GENIE_XSEC_FILE", "/opt/splines.xml")
    calls = []
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or type("R", (), {"returncode": 0})())
    import gdmltp.backends.genie_convert as gc
    monkeypatch.setattr(gc, "convert", lambda *a, **k: None)

    job = _write_job(tmp_path, flux={"mode": "exp", "value": "2 GeV",
                                     "min": "200 MeV", "max": "20 GeV", "bins": []})
    mod.run(str(job))
    gevgen = calls[0]
    assert gevgen[gevgen.index("-e") + 1] == "0.2,20"
    assert gevgen[gevgen.index("-f") + 1] == "exp(-x/2)"


def test_driver_generates_splines_on_demand(repo_root, tmp_path, monkeypatch):
    """With no explicit splines and no $GENIE_XSEC_FILE, the driver runs gmkspl
    once (nu + antinu on the target), caches the XML in the run dir, and points
    gevgen at it -- so a fresh image needs zero manual setup."""
    mod = _load_driver(repo_root)
    monkeypatch.delenv("GENIE_XSEC_FILE", raising=False)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[0] == "gmkspl":                       # gmkspl writes its output
            (tmp_path / cmd[cmd.index("-o") + 1].split("/")[-1]).write_text("<xml/>")
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    import gdmltp.backends.genie_convert as gc
    monkeypatch.setattr(gc, "convert", lambda *a, **k: None)

    assert mod.run(str(_write_job(tmp_path))) == 0
    gmkspl = calls[0]
    assert gmkspl[0] == "gmkspl"
    assert gmkspl[gmkspl.index("-p") + 1] == "14,-14"
    assert gmkspl[gmkspl.index("-t") + 1] == "1000180400"
    assert "--tune" in gmkspl
    gevgen = calls[1]
    assert gevgen[0] == "gevgen"
    xsec = gevgen[gevgen.index("--cross-sections") + 1]
    assert "gxspl_14_1000180400" in xsec

    # second run: spline file exists -> no second gmkspl
    calls.clear()
    assert mod.run(str(_write_job(tmp_path))) == 0
    assert calls[0][0] == "gevgen"


def test_driver_hedis_tune(repo_root, tmp_path, monkeypatch):
    """A GHE19/HEDIS tune: the driver builds structure-function tables first
    (gmkhedissf), then gmkspl and gevgen both carry --event-generator-list
    HEDIS (they must match), out to the TeV flux endpoint."""
    mod = _load_driver(repo_root)
    monkeypatch.delenv("GENIE_XSEC_FILE", raising=False)
    monkeypatch.setenv("HEDIS_SF_DATA_PATH", str(tmp_path / "sf"))  # empty -> build
    monkeypatch.setenv("GDMLTP_HEDIS", "1")   # a HEDIS-provisioned image
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[0] == "gmkspl":
            (tmp_path / cmd[cmd.index("-o") + 1].split("/")[-1]).write_text("<xml/>")
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    import gdmltp.backends.genie_convert as gc
    monkeypatch.setattr(gc, "convert", lambda *a, **k: None)

    job = _write_job(tmp_path, tune="GHE19_00a_00_000",
                     event_generator_list="HEDIS",
                     flux={"mode": "mono", "value": "5 TeV", "min": None,
                           "max": None, "bins": []},
                     flux_emax_gev=5000.0)
    assert mod.run(str(job)) == 0
    names = [c[0] for c in calls]
    assert names[0] == "gmkhedissf"                      # SF tables built first
    assert calls[0][calls[0].index("--tune") + 1] == "GHE19_00a_00_000"
    gmkspl = next(c for c in calls if c[0] == "gmkspl")
    assert gmkspl[gmkspl.index("--event-generator-list") + 1] == "HEDIS"
    assert gmkspl[gmkspl.index("-e") + 1] == "5000"      # TeV spline reach
    gevgen = next(c for c in calls if c[0] == "gevgen")
    assert gevgen[gevgen.index("--event-generator-list") + 1] == "HEDIS"


def test_driver_hedis_uses_baked_spline(repo_root, tmp_path, monkeypatch):
    """A provided HEDIS xsec spline (via HEDIS_XSEC_DIR, covering the requested
    energy), together with that tune's SF tables, is used directly -- no
    gmkhedissf, no gmkspl. The spline follows the driver's own cache naming."""
    mod = _load_driver(repo_root)
    monkeypatch.delenv("GENIE_XSEC_FILE", raising=False)
    monkeypatch.setenv("GDMLTP_HEDIS", "1")
    baked_dir = tmp_path / "hedis-xsec"
    baked_dir.mkdir()
    # a 5 TeV baked spline for numu on W-184, GHE19_00c / HEDIS
    (baked_dir / "gxspl_14_1000741840_GHE19_00c_00_000_HEDIS_5000gev.xml").write_text("<xml/>")
    monkeypatch.setenv("HEDIS_XSEC_DIR", str(baked_dir))
    # ...and SF tables for the same tune, as a user or custom image would
    # provide them. gevgen reads them from <HEDIS_SF_DATA_PATH>/<tune>/ during
    # event generation (not just gmkspl), so a provided spline alone is not
    # enough. Pointing HEDIS_SF_DATA_PATH at tmp_path also keeps the test from
    # probing or creating /opt/genie paths.
    _bake_sf_tables(tmp_path / "hedis-sf", "GHE19_00c_00_000")
    monkeypatch.setenv("HEDIS_SF_DATA_PATH", str(tmp_path / "hedis-sf"))
    calls = []
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or type("R", (), {"returncode": 0})())
    import gdmltp.backends.genie_convert as gc
    monkeypatch.setattr(gc, "convert", lambda *a, **k: None)

    job = _write_job(tmp_path, tune="GHE19_00c_00_000", target=1000741840,
                     probe=14, event_generator_list="HEDIS",
                     flux={"mode": "mono", "value": "3 TeV", "min": None,
                           "max": None, "bins": []}, flux_emax_gev=3000.0)
    assert mod.run(str(job)) == 0
    names = [c[0] for c in calls]
    assert "gmkspl" not in names and "gmkhedissf" not in names   # baked -> skipped
    gevgen = next(c for c in calls if c[0] == "gevgen")
    xsec = gevgen[gevgen.index("--cross-sections") + 1]
    assert xsec.endswith("gxspl_14_1000741840_GHE19_00c_00_000_HEDIS_5000gev.xml")


def test_driver_hedis_tune_unprovisioned_errors(repo_root, tmp_path, monkeypatch):
    """A HEDIS tune on an image NOT built with HEDIS (no GDMLTP_HEDIS marker)
    fails with a clear, actionable message before gmkhedissf can SIGABRT."""
    mod = _load_driver(repo_root)
    monkeypatch.delenv("GENIE_XSEC_FILE", raising=False)
    monkeypatch.delenv("GDMLTP_HEDIS", raising=False)          # non-HEDIS image
    monkeypatch.setenv("HEDIS_SF_DATA_PATH", str(tmp_path / "sf"))  # empty -> would build

    def fake_run(cmd, **kw):                                  # must NOT be reached
        raise AssertionError(f"subprocess should not run for {cmd}")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    job = _write_job(tmp_path, tune="GHE19_00a_00_000",
                     event_generator_list="HEDIS",
                     flux={"mode": "mono", "value": "5 TeV", "min": None,
                           "max": None, "bins": []},
                     flux_emax_gev=5000.0)
    with pytest.raises(RuntimeError, match="HEDIS"):
        mod.run(str(job))
