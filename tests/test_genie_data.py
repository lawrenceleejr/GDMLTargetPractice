"""gdmltp.genie_data: fetch, verify and wire up the GENIE HEDIS data. Runs
offline -- a tiny fake bundle is served from file:// URLs."""
import hashlib
import io
import tarfile

import pytest

from gdmltp import genie_data as g

BUNDLE = g.DEFAULT_BUNDLE
# A genie_xsec UPS tarball is laid out as <product>/<version>/NULL/<spline set>/data/,
# and the installer picks out that data/ directory by matching the manifest's
# member_dir. The set name is arbitrary: nothing here depends on which spline set
# (or energy range) it is, only that member_dir and the tarball agree.
SPLINE_SET = "TESTSET-k10-e100"
TARBALL_DATA_DIR = f"genie_xsec/v1_00_00/NULL/{SPLINE_SET}/data"


def _spline_xml(targets):
    return ("<genie_xsec_spline_list>\n" + "".join(
        f'<spline name="genie::HEDISPXSec/Default/nu:14;tgt:{t};N:2212;'
        f'proc:Weak[CC],DIS;" nknots="2"></spline>\n' for t in targets)
        + "</genie_xsec_spline_list>\n").encode()


def _tarball(path, members, links=None):
    """A bz2 tarball of `members` (name -> bytes) plus symlinks (name -> target)."""
    with tarfile.open(path, "w:bz2") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type, info.linkname = tarfile.SYMTYPE, target
            tf.addfile(info)
    return path


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """A mirror holding the HEDIS files, a good spline tarball, and a manifest
    pointing at both. Returns a dict of the paths involved."""
    mirror = tmp_path / "mirror"
    contents = {"hedis-sf/GHE19_00a_00_000/Inputs.txt": b"NNPDF31sx\n",
                "hedis-sf/GHE19_00a_00_000/QrkSF_LO_nu_cc_p.dat": b"q" * 5000,
                "photon-sf/PhotonSF_hitnuc2212_hitlep14.dat": b"p" * 300,
                "pdfs/SET/SET_0000.dat": b"d" * 100000}
    files = []
    for rel, data in contents.items():
        (mirror / rel).parent.mkdir(parents=True, exist_ok=True)
        (mirror / rel).write_bytes(data)
        files.append((rel, len(data), hashlib.sha256(data).hexdigest()))
    # Same shape as the real genie_xsec tarball: the NU files are real, the FNAL
    # names are symlinks to them.
    good = _tarball(tmp_path / "good.tar.bz2",
                    {f"{TARBALL_DATA_DIR}/gxspl-NUsmall.xml": _spline_xml([1000050110, 1000060120]),
                     f"{TARBALL_DATA_DIR}/README": b"readme\n",
                     "genie_xsec/v1_00_00.version/NULL": b"ups\n"},       # not installed
                    links={f"{TARBALL_DATA_DIR}/gxspl-FNALsmall.xml": "gxspl-NUsmall.xml",
                           f"{TARBALL_DATA_DIR}/escape": "../../../../etc/passwd"})  # must be skipped
    bundles = {BUNDLE: {
        "description": "test", "genie_version": "3.6.2", "tune": "GHE19_00a_00_000",
        "components": {
            "he_data": {"kind": "files", "commit": "abc1234def",
                        "base_url": mirror.as_uri() + "/", "cvmfs_dirs": [],
                        "files": files,
                        "env": {"HEDIS_SF_DATA_PATH": "hedis-sf",
                                "PHOTON_SF_DATA_PATH": "photon-sf", "LHAPATH": "pdfs"}},
            "splines": {"kind": "tarball", "url": good.as_uri(),
                        "sha256": g.sha256_file(good),
                        "member_dir": f"{SPLINE_SET}/data",
                        "default_xsec_file": "gxspl-NUsmall.xml"}}}}
    monkeypatch.setattr(g, "BUNDLES", bundles)
    return {"dest": tmp_path / "data", "mirror": mirror, "good": good,
            "bundles": bundles, "tmp": tmp_path}


def _env(dest):
    lines = (dest / "genie" / "genie-hedis.env").read_text().splitlines()
    return dict(l.split("=", 1) for l in lines if l and not l.startswith("#"))


def test_install_downloads_verifies_and_writes_env(fake):
    dest = fake["dest"]
    assert g.main(["install", "--dest", str(dest), "--jobs", "2"]) == 0
    root = dest / "genie" / BUNDLE
    assert (root / "he_data/hedis-sf/GHE19_00a_00_000/QrkSF_LO_nu_cc_p.dat").read_bytes() == b"q" * 5000
    assert (root / "splines/gxspl-NUsmall.xml").is_file()
    alias = root / "splines/gxspl-FNALsmall.xml"          # the tarball's alias survives
    assert alias.is_symlink() and alias.read_bytes() == (root / "splines/gxspl-NUsmall.xml").read_bytes()
    assert not (root / "splines/escape").exists()         # a link leaving the dir is dropped
    assert not (root / "splines/NULL").exists()           # only the data/ dir is installed
    env = _env(dest)
    assert env["HEDIS_SF_DATA_PATH"] == f"/data/gdmltp/genie/{BUNDLE}/he_data/hedis-sf"
    assert env["LHAPATH"] == f"/data/gdmltp/genie/{BUNDLE}/he_data/pdfs"
    assert env["GENIE_XSEC_FILE"] == f"/data/gdmltp/genie/{BUNDLE}/splines/gxspl-NUsmall.xml"
    sh = (dest / "genie" / "genie-hedis.sh").read_text()
    assert f'"${{_gdmltp_here}}/{BUNDLE}/splines/gxspl-NUsmall.xml"' in sh
    assert g.main(["status", "--dest", str(dest)]) == 0


def test_rerun_is_a_noop(fake, monkeypatch):
    dest = fake["dest"]
    assert g.main(["install", "--dest", str(dest)]) == 0
    monkeypatch.setattr(g, "download", lambda *a, **k: pytest.fail("re-downloaded"))
    assert g.main(["install", "--dest", str(dest)]) == 0


def test_matching_cvmfs_copy_is_used_in_place(fake, monkeypatch):
    monkeypatch.setattr(g, "fetch_files", lambda *a, **k: pytest.fail("downloaded HEDIS data"))
    cvmfs = fake["mirror"]                                  # identical to the pinned files
    assert g.main(["install", "--dest", str(fake["dest"]), "--cvmfs-dir", str(cvmfs)]) == 0
    env = _env(fake["dest"])
    assert env["HEDIS_SF_DATA_PATH"] == str(cvmfs / "hedis-sf")   # absolute, not the mount
    assert env["GENIE_XSEC_FILE"].startswith("/data/gdmltp/")      # splines still local


def test_different_cvmfs_copy_is_not_used(fake):
    other = fake["tmp"] / "other"
    for rel, _size, _sha in fake["bundles"][BUNDLE]["components"]["he_data"]["files"]:
        (other / rel).parent.mkdir(parents=True, exist_ok=True)
        (other / rel).write_bytes((fake["mirror"] / rel).read_bytes())
    (other / "hedis-sf/GHE19_00a_00_000/Inputs.txt").write_bytes(b"HERAPDF\n!!")  # other version
    assert g.main(["install", "--dest", str(fake["dest"]), "--cvmfs-dir", str(other)]) == 0
    assert _env(fake["dest"])["HEDIS_SF_DATA_PATH"].startswith("/data/gdmltp/")   # downloaded


def test_rejected_tarball_leaves_the_installed_set_untouched(fake):
    dest = fake["dest"]
    assert g.main(["install", "--dest", str(dest)]) == 0
    installed = dest / "genie" / BUNDLE / "splines" / "gxspl-NUsmall.xml"
    before = installed.read_bytes()
    other = _tarball(fake["tmp"] / "other.tar.bz2",
                     {f"{TARBALL_DATA_DIR}/gxspl-NUsmall.xml": _spline_xml([1000060120])})
    assert g.main(["install", "--dest", str(dest), "--spline-tarball", str(other)]) == 1
    assert installed.read_bytes() == before                 # pinned sha256 differs


def test_xsec_file_can_be_chosen_and_must_exist(fake, capsys):
    dest = fake["dest"]
    assert g.main(["install", "--dest", str(dest), "--xsec-file", "gxspl-FNALsmall.xml"]) == 0
    assert _env(dest)["GENIE_XSEC_FILE"].endswith("/splines/gxspl-FNALsmall.xml")
    assert g.main(["install", "--dest", str(dest), "--xsec-file", "gxspl-nope.xml"]) == 1
    err = capsys.readouterr().err
    assert "gxspl-nope.xml is not among the installed spline files" in err
    assert "gxspl-NUsmall.xml" in err and ".gdmltp" not in err   # lists real files only


def test_verify_checks_installed_splines_without_downloading(fake, monkeypatch):
    dest = fake["dest"]
    assert g.main(["install", "--dest", str(dest)]) == 0
    fake["good"].unlink()                                   # the source is now unreachable
    assert g.main(["install", "--dest", str(dest), "--verify"]) == 0   # intact: no download


def test_verify_reinstalls_damaged_splines(fake):
    dest = fake["dest"]
    assert g.main(["install", "--dest", str(dest)]) == 0
    installed = dest / "genie" / BUNDLE / "splines" / "gxspl-NUsmall.xml"
    good = installed.read_bytes()
    installed.write_bytes(b"corrupt")
    assert g.main(["install", "--dest", str(dest), "--verify"]) == 0
    assert installed.read_bytes() == good


def test_http_4xx_is_not_retried_but_5xx_is(tmp_path, monkeypatch):
    import urllib.error
    calls = []

    def refuse(code):
        def urlopen(req, timeout=None):
            calls.append(code)
            raise urllib.error.HTTPError(req.full_url, code, "nope", {}, None)
        return urlopen

    monkeypatch.setattr(g.time, "sleep", lambda s: None)
    monkeypatch.setattr(g.urllib.request, "urlopen", refuse(404))
    with pytest.raises(g.DataError, match="HTTP 404"):
        g.download("https://example.invalid/f", tmp_path / "f", retries=3)
    assert calls == [404]                                    # one attempt, no retries
    calls.clear()
    monkeypatch.setattr(g.urllib.request, "urlopen", refuse(503))
    with pytest.raises(g.DataError, match="after 3 attempts"):
        g.download("https://example.invalid/f", tmp_path / "f", retries=3)
    assert calls == [503, 503, 503]


def test_wrong_tarball_checksum_is_rejected(fake):
    fake["good"].write_bytes(fake["good"].read_bytes() + b"x")      # no longer the pinned file
    assert g.main(["install", "--dest", str(fake["dest"])]) == 1
    assert not (fake["dest"] / "genie" / BUNDLE / "splines").exists()


def test_unpinned_tarball_download_requires_opt_in(fake):
    fake["bundles"][BUNDLE]["components"]["splines"]["sha256"] = None
    assert g.main(["install", "--dest", str(fake["dest"])]) == 1
    assert g.main(["install", "--dest", str(fake["dest"]), "--allow-unverified"]) == 0


def test_failed_download_leaves_no_partial_file(fake, tmp_path):
    src = tmp_path / "f.bin"
    src.write_bytes(b"abc")
    out = tmp_path / "out" / "f.bin"
    with pytest.raises(g.DataError):
        g.download(src.as_uri(), out, sha256="0" * 64, retries=1)
    assert not out.exists() and not out.with_name("f.bin.part").exists()


def test_check_detects_a_changed_file(fake, capsys):
    mirror = fake["mirror"]
    assert g.main(["check", str(mirror)]) == 0
    f = mirror / "pdfs/SET/SET_0000.dat"
    f.write_bytes(b"e" * 100000)                            # same size, different content
    assert g.main(["check", str(mirror), "--quick"]) == 0   # sizes only can't see it
    assert g.main(["check", str(mirror)]) == 1
    assert "content differs: pdfs/SET/SET_0000.dat" in capsys.readouterr().out


def _refuse_python(monkeypatch, code=403):
    import urllib.error

    def urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, code, "Forbidden", {}, None)
    monkeypatch.setattr(g.urllib.request, "urlopen", urlopen)


def _fake_tools(monkeypatch, payload, available=("curl",)):
    """Pretend curl/wget exist; the 'download' writes `payload` to the -o/-O path."""
    ran = []
    monkeypatch.setattr(g.shutil, "which", lambda t: f"/usr/bin/{t}" if t in available else None)

    def run(cmd, **kw):
        ran.append(cmd[0])
        out = cmd[cmd.index("-o" if cmd[0] == "curl" else "-O") + 1]
        with open(out, "wb") as f:
            f.write(payload)
        return type("R", (), {"returncode": 0, "stderr": ""})()
    monkeypatch.setattr(g.subprocess, "run", run)
    return ran


def test_403_falls_back_to_curl_and_still_verifies(tmp_path, monkeypatch):
    _refuse_python(monkeypatch)
    data = b"spline tarball bytes"
    ran = _fake_tools(monkeypatch, data)
    out = tmp_path / "t.tar.bz2"
    digest = g.download("https://scisoft.example/t.tar.bz2", out,
                        sha256=hashlib.sha256(data).hexdigest())
    assert ran == ["curl"] and out.read_bytes() == data
    assert digest == hashlib.sha256(data).hexdigest()


def test_403_fallback_rejects_wrong_content(tmp_path, monkeypatch):
    _refuse_python(monkeypatch)
    _fake_tools(monkeypatch, b"an error page, not the tarball", available=("wget",))
    out = tmp_path / "t.tar.bz2"
    with pytest.raises(g.DataError, match="checksum mismatch"):
        g.download("https://scisoft.example/t.tar.bz2", out, sha256="0" * 64)
    assert not out.exists() and not out.with_name("t.tar.bz2.part").exists()


def test_403_without_curl_or_wget_explains(tmp_path, monkeypatch):
    _refuse_python(monkeypatch)
    _fake_tools(monkeypatch, b"", available=())
    with pytest.raises(g.DataError, match="neither curl nor wget"):
        g.download("https://scisoft.example/t.tar.bz2", tmp_path / "t", sha256="0" * 64)