# GENIE base image for GDMLTargetPractice, built entirely from source so users
# never compile anything themselves. Provides gevgen/gmkspl/gntpc on PATH with
# the full dependency stack:
#
#   Pythia6 (via GENIE's own ext build script)
#   ROOT (compiled with Pythia6 + MathMore + GDML, batch-only)
#   LHAPDF 6
#   log4cpp / libxml2 / GSL (apt)
#   GENIE Generator (pinned release)
#
# Built by .github/workflows/build-generator-bases.yml (slow: ~2-3 h) and
# published as ghcr.io/<owner>/gdmltargetpractice-genie-base; the fast per-push
# genie app image (docker/genie.Dockerfile) layers gdmltp + the driver on top.
ARG UBUNTU=ubuntu:22.04
FROM ${UBUNTU}
# JTR: Updating GENIE to 3.06.02 and LHAPDF to 6.5.6
ARG GENIE_VERSION=R-3_06_02
ARG ROOT_VERSION=6.28.12
ARG LHAPDF_VERSION=6.5.6

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential gfortran cmake git wget curl ca-certificates \
    python3 python3-pip python3-dev \
    libgsl-dev libxml2-dev liblog4cpp5-dev \
    libpcre3-dev zlib1g-dev libbz2-dev liblzma-dev libzstd-dev \
    libssl-dev libffi-dev rsync \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt

# --- GENIE source first: its ext scripts build Pythia6 for us --------------
RUN git clone --branch ${GENIE_VERSION} --depth 1 \
      https://github.com/GENIE-MC/Generator.git /opt/genie


# JTR: Changing to Pythia8 as this is the GENIE recommendation for the future
# --- Pythia8 -----------------------------------------------------------------
RUN mkdir -p /opt/pythia8 && cd /opt/pythia8 && \
    wget https://pythia8.web.cern.ch/releases/pythia83/pythia8318.tgz && \
    tar -xf pythia8318.tgz && rm pythia8318.tgz && \
    cd pythia8318 && \
    ./configure --prefix=/opt/pythia8/install && \
    make -j"$(nproc)" && make install && \
    cd /opt && rm -rf /opt/pythia8/pythia8318
ENV PYTHIA8_DIR=/opt/pythia8/install
ENV PYTHIA8_INC=${PYTHIA8_DIR}/include
ENV PYTHIA8_LIB=${PYTHIA8_DIR}/lib

# --- ROOT with Pythia8 support (batch-only: no graphics, no PyROOT) --------
RUN wget -q https://root.cern/download/root_v${ROOT_VERSION}.source.tar.gz && \
    tar -xzf root_v${ROOT_VERSION}.source.tar.gz && rm root_v${ROOT_VERSION}.source.tar.gz && \
    mkdir root-build && cd root-build && \
    cmake ../root-${ROOT_VERSION} \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_INSTALL_PREFIX=/opt/root \
      -Dpythia8=ON \
      -DPYTHIA8_DIR=/opt/pythia8/install \
      -DPYTHIA8_INCLUDE_DIR=/opt/pythia8/install/include \
      -DPYTHIA8_LIBRARY=/opt/pythia8/install/lib/libpythia8.so \
      -Dmathmore=ON -Dgdml=ON -Dminuit2=ON \
      -Dbuiltin_gsl=OFF \
      -Dx11=OFF -Dopengl=OFF -Dwebgui=OFF -Droot7=OFF \
      -Dxrootd=OFF -Ddavix=OFF -Dtmva=OFF -Droofit=OFF \
      -Dpyroot=OFF -Dpython=OFF \
      -Dfitsio=OFF -Dmysql=OFF -Dpgsql=OFF -Dsqlite=OFF -Ddcache=OFF \
      -Dimt=ON && \
    cmake --build . -j"$(nproc)" && cmake --install . && \
    cd /opt && rm -rf root-build root-${ROOT_VERSION}
ENV ROOTSYS=/opt/root
ENV PATH=${ROOTSYS}/bin:${PATH}
ENV LD_LIBRARY_PATH=${ROOTSYS}/lib:${PYTHIA8_LIB}

# JTR: Modernizing to LHAPDF 6.5.6
# --- LHAPDF 6.5.6 ------------------------------------------------------------
RUN wget -q https://lhapdf.hepforge.org/downloads/?f=LHAPDF-6.5.6.tar.gz \
      -O LHAPDF-6.5.6.tar.gz && \
    tar -xzf LHAPDF-6.5.6.tar.gz && rm LHAPDF-6.5.6.tar.gz && \
    cd LHAPDF-6.5.6 && \
    ./configure --prefix=/opt/lhapdf PYTHON=python3 && \
    make -j"$(nproc)" && make install && \
    cd /opt && rm -rf LHAPDF-6.5.6
ENV LHAPDF_DIR=/opt/lhapdf
ENV LD_LIBRARY_PATH=${LHAPDF_DIR}/lib:${LD_LIBRARY_PATH}
ENV PATH=${LHAPDF_DIR}/bin:${PATH}
ENV LHAPDF_DATA_PATH=${LHAPDF_DIR}/share/LHAPDF
ENV LHAPATH=${LHAPDF_DIR}/share/LHAPDF

# --- APFEL (OPTIONAL: only for HEDIS high-energy-DIS tunes) -------------------
# ENABLE_HEDIS=0 (default) builds nothing here and leaves the GENIE configure
# below byte-for-byte unchanged, so the proven default image is unaffected.
# ENABLE_HEDIS=1 additionally builds APFEL (needed for the NLO structure
# functions of the GHE19 tunes) -- see the multi-TeV neutrino examples.
ARG ENABLE_HEDIS=0
ARG APFEL_VERSION=3.0.6
RUN if [ "$ENABLE_HEDIS" = "1" ]; then \
      command -v python >/dev/null 2>&1 || \
        ln -sf "$(command -v python3)" /usr/local/bin/python && \
      wget -q https://github.com/scarrazza/apfel/archive/refs/tags/${APFEL_VERSION}.tar.gz \
        -O apfel.tar.gz && tar -xzf apfel.tar.gz && rm apfel.tar.gz && \
      cd apfel-${APFEL_VERSION} && ./configure --prefix=/opt/apfel && \
      make -j"$(nproc)" && make install && cd /opt && rm -rf apfel-${APFEL_VERSION} ; \
    fi
ENV APFEL_DIR=/opt/apfel
ENV LD_LIBRARY_PATH=${APFEL_DIR}/lib:${LD_LIBRARY_PATH}
ENV PATH=${APFEL_DIR}/bin:${PATH}

# --- GENIE Generator ----------------------------------------------------------
# In-place build (the GENIE convention: $GENIE holds source, bin/ and lib/).
# The default G18 tunes use GENIE's native GRV98LO, so no external PDF data
# files are required at run time. With ENABLE_HEDIS=1, --enable-apfel is added
# so the high-energy-DIS (GHE19/HEDIS) tunes are available too.
ENV GENIE=/opt/genie
# Bump GENIE_SRC_REV to force a genuine recompile when a GENIE source patch
# below changes. A separate `RUN sed ...` before the build does NOT reliably
# bust the registry build cache: BuildKit's cache-from matched the old compiled
# `make` layer even with a new patch layer above it, so the patched source was
# never actually recompiled (the baked HEDIS metafile came out at old, low
# precision). Keeping the patch and the build in ONE RUN -- keyed by this rev --
# guarantees the compile sees the patched source. Bump on any source-patch edit.\

# JTR: Changed this 2 -> 3 to demand recompilation of GENIE
ARG GENIE_SRC_REV=3
# Patch a GENIE HEDIS metafile round-trip bug: HEDISStrucFunc's operator<<
# writes the SF Inputs.txt at default (~6 sig-fig) precision, but operator==
# compares the re-read metafile against the tune config at 1e-10. Tunes whose
# masses/couplings carry more digits (e.g. GHE19_00c: MassW=79.177...) then
# fail the "Info from MetaFile and Tune doesnt match" assertion in gevgen even
# though the SF tables are valid. Write full precision so the round-trip is
# exact. (Verified end-to-end: this is exactly what blocks GHE19_00c event
# generation, and setprecision(15) clears it.) The patch runs in the SAME RUN
# as configure+make so the fix is always compiled in (see GENIE_SRC_REV above).

# JTR: added --enable-fnal to the list of configuration commands. Added
# the liblog line because I needed it in my experience. Note the find and sed
# lines patching out references to pythia6 and replacing with pythia8. Updated
# configuration flags to include baryon resonance, validation tools, and
# pythia6->8
RUN echo "GENIE_SRC_REV=${GENIE_SRC_REV}" && \
    apt-get update && apt-get install -y liblog4cpp5-dev && \
    cd ${GENIE}/config && \
    find . -name "*.xml" -exec sed -i 's/Pythia6Decayer2023/Pythia8Decayer2023/g' {} + && \
    find . -name "*.xml" -exec sed -i 's/AGCharmPythia6Hadro2023/AGCharmPythia8Hadro2023/g' {} + && \
    find . -name "*.xml" -exec sed -i 's/Pythia6/Pythia8/g' {} + && \
    cd ${GENIE} && \
    sed -i '/#include <fstream>/a #include <iomanip>' \
      src/Physics/HEDIS/XSection/HEDISStrucFunc.h && \
    sed -i 's/return os <</return os << std::setprecision(15) <</' \
      src/Physics/HEDIS/XSection/HEDISStrucFunc.h && \
    grep -n "setprecision" src/Physics/HEDIS/XSection/HEDISStrucFunc.h && \
    HEDIS_CFG="" && \
    if [ "$ENABLE_HEDIS" = "1" ]; then \
      HEDIS_CFG="--enable-apfel --with-apfel-inc=${APFEL_DIR}/include --with-apfel-lib=${APFEL_DIR}/lib" ; \
    fi && \
    ./configure \
      --enable-fnal \
      --enable-baryon-residence \
      --enable-validation-tools \
      --disable-pythia6 \
      --enable-pythia8 \
      --disable-lhapdf5 \
      --enable-lhapdf6 \
      --with-pythia8-inc=${PYTHIA8_INC} \
      --with-pythia8-lib=${PYTHIA8_LIB} \
      --with-lhapdf6-inc=${LHAPDF_DIR}/include \
      --with-lhapdf6-lib=${LHAPDF_DIR}/lib \
      --with-optiz-level=O2 \
      --with-log4cpp-inc=/usr/include \
      --with-log4cpp-lib=/usr/lib/x86_64-linux-gnu \
      --with-libxml2-inc=/usr/include/libxml2 \
      --with-libxml2-lib=/usr/lib/x86_64-linux-gnu \
      --enable-flux-drivers --enable-geom-drivers \
      --disable-profiler --disable-doxygen-doc \
      ${HEDIS_CFG} && \
    make -j"$(nproc)" && \
    find ${GENIE} -name '*.o' -delete
ENV PATH=${GENIE}/bin:${PATH}
ENV LD_LIBRARY_PATH=${GENIE}/lib:${LD_LIBRARY_PATH}

# --- HEDIS inputs (OPTIONAL) -------------------------------------------------
# Bake in the PDF grid + structure-function tables for the reference HEDIS tune
# so a HEDIS run needs no manual setup. Only runs when ENABLE_HEDIS=1.
#
# Default to the LO tune GHE19_00c_00_000. Its config asks for LHAPDF set
# 'cteq6', which is NOT in the public LHAPDF distribution -- but the compatible
# LO CTEQ6 grid 'cteq6l1' IS on the LHAPDF server, so fetch that and point the
# tune at it. (This recipe was validated end-to-end: gmkhedissf builds the full
# QrkSF_LO_* table set.) LO needs no NLO structure functions; APFEL is still
# built above so a correct NLO PDF could be dropped in and the tune switched.
ARG HEDIS_TUNE=GHE19_00c_00_000
# The tune's config lives in $GENIE/config/<HEDIS_TUNE_CONFIG>/ -- that dir is
# named for the model (GHE19_00c), NOT the full tune string (GHE19_00c_00_000).
ARG HEDIS_TUNE_CONFIG=GHE19_00c
ARG HEDIS_PDF=cteq6l1
ARG LHAPDF_SETS_URL=https://lhapdfsets.web.cern.ch/current
# gmkhedissf writes the QrkSF tables under $HEDIS_SF_DATA_PATH/<tune>/; the
# gdmltp genie driver reads the same path at run time (its HEDIS_SF_DATA_PATH
# default), so a baked-in tune needs no on-demand rebuild.
ENV HEDIS_SF_DATA_PATH=${GENIE}/data/evgen/hedis-sf
RUN if [ "$ENABLE_HEDIS" = "1" ]; then \
      set -e && \
      DD="$(lhapdf-config --datadir)" && mkdir -p "$DD" && \
      wget -q --tries=5 --retry-connrefused --waitretry=20 --timeout=60 \
        "${LHAPDF_SETS_URL}/${HEDIS_PDF}.tar.gz" -O /tmp/pdf.tar.gz && \
      tar -xzf /tmp/pdf.tar.gz -C "$DD" && rm /tmp/pdf.tar.gz && \
      test -e "$DD/${HEDIS_PDF}/${HEDIS_PDF}.info" \
        || { echo "ERROR: LHAPDF set ${HEDIS_PDF} missing after download"; ls -la "$DD"; exit 1; } && \
      sed -i 's/>[[:space:]]*cteq6[[:space:]]*</> '"${HEDIS_PDF}"' </' \
        "${GENIE}/config/${HEDIS_TUNE_CONFIG}/CommonParam.xml" && \
      grep -n "LHAPDF-set" "${GENIE}/config/${HEDIS_TUNE_CONFIG}/CommonParam.xml" && \
      mkdir -p "$HEDIS_SF_DATA_PATH" && cd "$HEDIS_SF_DATA_PATH" && \
      gmkhedissf --tune "${HEDIS_TUNE}" && \
      echo "HEDIS SF tables built:" && find "$HEDIS_SF_DATA_PATH" -name 'QrkSF*' | head ; \
    fi
# Bake a HEDIS cross-section spline for the shipped MAIA example (nu_mu + bar on
# W-184, GHE19_00c / HEDIS, up to 5 TeV) so it runs without the slow on-demand
# gmkspl. HEDIS xsec integration is genuinely expensive -- a full-quality spline
# is many hours, more than a CI build can afford -- so this is BEST-EFFORT and
# NON-FATAL: a coarse spline (few knots; gevgen accepts it, verified) under a
# hard time budget that keeps the build inside its cap. If it doesn't finish it
# is skipped and the driver falls back to on-demand generation (which caches in
# the run dir). The filename follows the driver's cache convention so
# _baked_hedis_spline() in run_genie.py finds it. Knot count / budget are ARGs
# so a beefier offline build can raise them.
ENV HEDIS_XSEC_DIR=${GENIE}/data/evgen/hedis-xsec
ARG HEDIS_XSEC_PROBE=14
ARG HEDIS_XSEC_TARGET=1000741840
ARG HEDIS_XSEC_EMAX=5000
ARG HEDIS_XSEC_KNOTS=3
ARG HEDIS_XSEC_TIMEOUT=14400
# Prefer a ready-made spline over computing one: GENIE's own pre-computed HEDIS
# splines are published on CVMFS only (no public HTTP download -- the
# GENIE-HEDIS fork that shipped XML/ROOT tables is gone and tunes.genie-mc.org
# is offline), so point this at a spline you host yourself to skip gmkspl
# entirely. Users with /cvmfs mounted can instead just set
# genie.cross_sections to the CVMFS path at run time (see docs/neutrino.md).
ARG HEDIS_XSEC_URL=
RUN if [ "$ENABLE_HEDIS" = "1" ]; then \
      mkdir -p "$HEDIS_XSEC_DIR" && cd "$HEDIS_XSEC_DIR" && \
      OUT="gxspl_${HEDIS_XSEC_PROBE}_${HEDIS_XSEC_TARGET}_${HEDIS_TUNE}_HEDIS_${HEDIS_XSEC_EMAX}gev.xml" && \
      if [ -n "${HEDIS_XSEC_URL}" ]; then \
        echo "[hedis] fetching prebuilt xsec spline from ${HEDIS_XSEC_URL}" && \
        wget -q --tries=5 --retry-connrefused --waitretry=20 --timeout=60 \
          "${HEDIS_XSEC_URL}" -O "$OUT" && \
        ls -la "$HEDIS_XSEC_DIR" ; \
      else \
      echo "[hedis] baking xsec spline (best-effort, timeout ${HEDIS_XSEC_TIMEOUT}s): $OUT" && \
      if timeout "${HEDIS_XSEC_TIMEOUT}" gmkspl \
            -p "${HEDIS_XSEC_PROBE},-${HEDIS_XSEC_PROBE}" -t "${HEDIS_XSEC_TARGET}" \
            --tune "${HEDIS_TUNE}" --event-generator-list HEDIS \
            -n "${HEDIS_XSEC_KNOTS}" -e "${HEDIS_XSEC_EMAX}" -o "$OUT" ; then \
        echo "[hedis] baked xsec spline:" && ls -la "$HEDIS_XSEC_DIR" ; \
      else \
        echo "[hedis] xsec spline bake did not finish in budget; removing partial, driver will build on demand" && \
        rm -f "$OUT" ; \
      fi ; \
      fi ; \
    fi
# Runtime marker: the gdmltp genie driver checks this before running gmkhedissf
# so a HEDIS tune on a non-HEDIS image fails with a clear message, not SIGABRT.
ENV GDMLTP_HEDIS=${ENABLE_HEDIS}

# Sanity: the generator toolchain must resolve.
RUN gevgen --help >/dev/null 2>&1 || gevgen -h >/dev/null 2>&1 || \
    ldd ${GENIE}/bin/gevgen

WORKDIR /work
