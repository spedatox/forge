# Centurion's Cell image — a comprehensive Kali toolbox baked at BUILD time so a
# live Cell pays nothing at job time. Cells are throwaway per job; without this
# bake Centurion re-`apt install`s its toolchain on every single engagement.
#
# forge/agents/centurion/profile.toml [cell].image points at the tag this
# produces. The per-agent image wins over the global FORGE_CELL_IMAGE, so no
# .env.centurion override is needed. Under the subprocess backend (Docker-less
# host) the image is ignored entirely — this matters only with the Docker Cell.
#
# Build (on the host, context is deploy/ to keep the .venv out of the daemon):
#   cd /opt/forge-mk1
#   docker build -f deploy/cell-centurion.Dockerfile -t forge-cell-centurion:latest deploy/
#
# It is large by design (kali-linux-large WITH recommends = the full pentest
# superset). Expect a multi-GB image and a long first build.
FROM kalilinux/kali-rolling

LABEL org.opencontainers.image.title="forge-cell-centurion" \
      org.opencontainers.image.description="Comprehensive Kali toolchain baked for the Forge's Centurion agent" \
      maintainer="spedatox"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# 1) Refresh the base, then install the LARGE metapackage WITH recommends (the
#    broad pentest superset — this is what makes the bake comprehensive), the
#    wordlist/SecLists collections, and a build toolchain for anything a job
#    wants to compile or pipx-install later. A failure here MUST break the build.
RUN apt-get update \
 && apt-get -y dist-upgrade \
 && apt-get -y install \
        kali-linux-large \
        seclists wordlists \
        nuclei feroxbuster amass \
        metasploit-framework \
        exploitdb \
        python3-pip python3-venv pipx golang-go \
        git curl wget jq unzip \
        iproute2 iputils-ping dnsutils netcat-traditional \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# 2) Decompress rockyou so it is usable out of the box (Kali ships it gzipped).
RUN if [ -f /usr/share/wordlists/rockyou.txt.gz ]; then \
        gunzip -k /usr/share/wordlists/rockyou.txt.gz; \
    fi

# 3) Warm the caches the first real job would otherwise pay for: nuclei's
#    template repo and the SearchSploit database. Both are soft — a warm step must
#    never fail the bake, so each is guarded. (Metasploit is intentionally NOT
#    warmed here: msfconsole wants a writable HOME/DB that a build layer lacks, so
#    it errors noisily for no gain. Its module cache builds on first real use.)
RUN nuclei -update-templates -silent || true
RUN searchsploit -u || true

# Cells are launched with `--workdir /workspace` and an explicit `sleep infinity`
# by the Docker Cell, so this CMD is only a sensible default for manual runs.
WORKDIR /workspace
CMD ["sleep", "infinity"]
