# GENIE backend image for GDMLTargetPractice -- ONE image for the whole
# neutrino pipeline: GENIE generates the interaction AND Geant4 transports the
# final state, so a single bare `docker run <this image> run --config nu.yaml
# -o out` produces the finished output.root with no second container.
#
# Layering (the audited design, not two ROOT builds fighting over one prefix):
#
#   FROM  the project's Geant4 image           g4sim + Geant4 + ROOT 6.26 +
#         (ghcr.io/<owner>/gdmltargetpractice) HepMC3 + gdmltp, env as shipped
#   COPY  the GENIE stack from genie-base at   /opt/{genie,root,pythia8,lhapdf,
#         its ORIGINAL /opt paths              apfel}: GENIE's own ROOT 6.28
#                                              (pythia8=ON), never relocated
#
# The two ROOT stacks NEVER share an environment. ROOT libraries carry
# unversioned sonames (libCore.so), so putting /opt/root/lib on the global
# LD_LIBRARY_PATH would make g4sim load GENIE's ROOT and crash. Instead the
# GENIE environment lives in /opt/genie-env.sh, entered only two ways:
#   * /usr/local/bin/{gevgen,gmkspl,gntpc,gmkhedissf,gspl2root} are shims that
#     source it and exec the real tool (for humans and CI smoke tests);
#   * genie/run_genie.py sources it into its own process at startup, so every
#     GENIE subprocess inherits it -- while g4sim, launched by a different
#     process, keeps the base image's ROOT 6.26 environment untouched.
#
# The g4sim binary and the gdmltp package are REBUILT here from this checkout,
# so this image is always current even though the Geant4 base tag may lag a
# push (both workflows fire on the same commit; this one cannot wait for the
# other). The heavy stack (Geant4/ROOT/HepMC3/Qt) comes from the base; the app
# layer is minutes.
#
# Override the bases:
#   docker build --build-arg GEANT4_BASE=<geant4 image> \
#                --build-arg GENIE_BASE=<genie base> ...
ARG GENIE_BASE=ghcr.io/lawrenceleejr/gdmltargetpractice-genie-base:latest
ARG GEANT4_BASE=ghcr.io/lawrenceleejr/gdmltargetpractice:main

# --- stage: the GENIE stack, plus markers the merged image needs ------------
FROM ${GENIE_BASE} AS genie-stack
# Persist whether this base was built with HEDIS (APFEL + SF tables): the
# GDMLTP_HEDIS ENV does not survive a COPY --from, a file does. apfel/ may not
# exist on non-HEDIS bases, and COPY of a missing dir is a hard error.
RUN printf '%s' "${GDMLTP_HEDIS:-0}" > /opt/genie/.gdmltp-hedis && \
    mkdir -p /opt/apfel

# --- the merged image --------------------------------------------------------
FROM ${GEANT4_BASE}

WORKDIR /app

# GENIE's runtime shared libraries (GSL, log4cpp, libxml2, gfortran for
# Pythia6). The -dev names are release-stable and pull the runtime libs.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgsl-dev liblog4cpp5-dev libxml2-dev libgfortran5 \
    && rm -rf /var/lib/apt/lists/*

# The GENIE stack keeps its original prefixes (its binaries may carry absolute
# RPATHs into /opt/*), so those paths must be FREE in the Geant4 base. If this
# ever fires, the base image changed shape -- relocate deliberately, don't merge.
RUN for d in /opt/genie /opt/root /opt/pythia8 /opt/lhapdf /opt/apfel; do \
      if [ -e "$d" ]; then \
        echo "COLLISION: $d already exists in the Geant4 base image" >&2; \
        exit 1; \
      fi; \
    done

COPY --from=genie-stack /opt/pythia8 /opt/pythia8
COPY --from=genie-stack /opt/root    /opt/root
COPY --from=genie-stack /opt/lhapdf  /opt/lhapdf
COPY --from=genie-stack /opt/apfel   /opt/apfel
COPY --from=genie-stack /opt/genie   /opt/genie

# The GENIE environment, entered per-process (see header). Deliberately NOT
# global ENV: the global environment stays the Geant4 base's, so g4sim links
# and runs against its own ROOT untouched.
RUN { \
      echo '# Environment for GENIE and ITS OWN ROOT/Pythia8/LHAPDF stack.'; \
      echo '# Source this (or use the /usr/local/bin shims) before running'; \
      echo '# gevgen/gmkspl/gntpc/gmkhedissf. Deliberately not global: the'; \
      echo '# transport engine (g4sim) links the Geant4 base ROOT, and ROOT'; \
      echo '# sonames are unversioned -- one shared library path would mix them.'; \
      echo 'export GENIE=/opt/genie'; \
      echo 'export PYTHIA8_DIR=/opt/pythia8/install'; \
      echo 'export PYTHIA8_LIB=/opt/pythia8/install/lib'; \
      echo 'export ROOTSYS=/opt/root'; \
      echo 'export LHAPDF_DIR=/opt/lhapdf'; \
      echo 'export APFEL_DIR=/opt/apfel'; \
      echo 'export LHAPDF_DATA_PATH=/opt/lhapdf/share/LHAPDF'; \
      echo 'export LHAPATH=/opt/lhapdf/share/LHAPDF'; \
      echo 'export HEDIS_SF_DATA_PATH=${GENIE}/data/evgen/hedis-sf'; \
      echo 'export HEDIS_XSEC_DIR=${GENIE}/data/evgen/hedis-xsec'; \
      echo 'export GDMLTP_HEDIS="$(cat /opt/genie/.gdmltp-hedis 2>/dev/null || echo 0)"'; \
      echo 'export PATH=${GENIE}/bin:${ROOTSYS}/bin:${LHAPDF_DIR}/bin:${APFEL_DIR}/bin:${PATH}'; \
      echo 'export LD_LIBRARY_PATH=${GENIE}/lib:${ROOTSYS}/lib:${PYTHIA8_LIB}:${LHAPDF_DIR}/lib:${APFEL_DIR}/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}'; \
    } > /opt/genie-env.sh

# Shims: the GENIE tools by their usual names, each entering the GENIE env
# first -- `docker run --entrypoint gevgen` and interactive debugging just work.
RUN { \
      echo '#!/bin/bash'; \
      echo 'source /opt/genie-env.sh'; \
      echo 'exec "$GENIE/bin/$(basename "$0")" "$@"'; \
    } > /usr/local/bin/genie-tool && \
    chmod +x /usr/local/bin/genie-tool && \
    for b in gevgen gevgen_fnal gmkspl gntpc gmkhedissf gspl2root; do \
      ln -sf genie-tool /usr/local/bin/$b; \
    done

# --- app layer: rebuilt from THIS checkout (see header) ----------------------
COPY g4sim/ /app/g4sim/
RUN rm -rf /app/build && mkdir -p /app/build && cd /app/build && \
    cmake /app/g4sim/ && cmake --build . -j"$(nproc)"

COPY gdmltp/ /app/pysrc/gdmltp/
COPY g4tp/ /app/pysrc/g4tp/
COPY pyproject.toml README.md /app/pysrc/
RUN python3 -m pip install --no-cache-dir /app/pysrc && \
    python3 -c "import gdmltp, pyhepmc; from gdmltp import handoff; from gdmltp.backends import genie_convert, achilles_convert; print('gdmltp', gdmltp.__version__, 'pyhepmc', pyhepmc.__version__)"

# GENIE driver (reads genie_job.json, runs gevgen -> gntpc -> genie2root; it
# sources /opt/genie-env.sh itself, so its GENIE subprocesses see that stack).
COPY genie/ /app/genie/

# Both engines must link cleanly IN THE SAME IMAGE -- that is the whole point.
RUN bash -c 'set -e; source /opt/genie-env.sh; ! ldd "$GENIE/bin/gevgen" | grep "not found"' && \
    bash -c 'set -e; ! ldd /app/build/g4sim | grep "not found"' && \
    echo "gevgen + g4sim both resolve in one image"

# Shared argument-shape dispatcher: *.mac -> g4sim, *.json -> the GENIE driver,
# else -> gdmltp. With both engines aboard, `run --config nu.yaml` completes the
# generator AND transport stages in this one container.
COPY g4sim/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh /app/genie/*.py
ENTRYPOINT ["/app/entrypoint.sh"]
