#!/usr/bin/env python3
"""Regenerate the pinned file list in gdmltp/genie_data_manifest.py from a
clone of the upstream HEDIS data repository
(https://github.com/pochoarus/genie_he_data).

Maintainers only: run this when moving to a new upstream commit or adding a
tune, then review the diff. Users never run it.

    git clone https://github.com/pochoarus/genie_he_data /tmp/genie_he_data
    python3 tools/make_genie_data_manifest.py /tmp/genie_he_data > /tmp/files.py

and paste HE_DATA_COMMIT / HE_DATA_FILES from /tmp/files.py into the manifest.
The clone's HEAD commit is what gets pinned. TUNE and PDF_SET choose what goes
into the bundle; PDF_SET must match the "LHAPDF set" line of
hedis-sf/<TUNE>/Inputs.txt (checked below).
"""
import hashlib
import pprint
import subprocess
import sys

TUNE = "GHE19_00a_00_000"
PDF_SET = "NNPDF31sx_nlo_as_0118_LHCb_nf_6"
PREFIXES = (f"hedis-sf/{TUNE}/", "photon-sf/", f"pdfs/{PDF_SET}/")


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True,
                          capture_output=True).stdout


def main(repo):
    commit = git(repo, "rev-parse", "HEAD").decode().strip()
    inputs = git(repo, "show", f"HEAD:hedis-sf/{TUNE}/Inputs.txt").decode()
    if PDF_SET not in inputs.split():
        sys.exit(f"hedis-sf/{TUNE}/Inputs.txt does not name {PDF_SET}; fix PDF_SET")
    files = []
    for line in git(repo, "ls-tree", "-r", "HEAD").decode().splitlines():
        meta, path = line.split("\t", 1)
        _mode, kind, obj = meta.split()
        if kind != "blob" or not path.startswith(PREFIXES):
            continue
        data = git(repo, "cat-file", "blob", obj)
        files.append((path, len(data), hashlib.sha256(data).hexdigest()))
    files.sort()
    print(f"HE_DATA_COMMIT = {commit!r}")
    print(f"HE_DATA_FILES = {pprint.pformat(files, width=100)}")


if __name__ == "__main__":
    main(sys.argv[1])
