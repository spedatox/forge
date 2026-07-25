# Centurion's Cell image — a HEADLESS Kali toolbox (no GUI, no X11) baked at BUILD
# time so a live Cell pays nothing at job time for the common tools. Cells are
# throwaway per job; Centurion runs as root WITH network in-cell, so anything not
# baked here he simply `apt-get install`s during the job — the bake only needs to
# cover the routine toolchain, not every tool in Kali.
#
# forge/agents/centurion/profile.toml [cell].image points at the tag this builds.
# The per-agent image wins over the global FORGE_CELL_IMAGE, so no .env.centurion
# override is needed. Under the subprocess backend (Docker-less host) the image is
# ignored entirely — this matters only with the Docker Cell.
#
# Build (on the host, context is deploy/ to keep the .venv out of the daemon):
#   cd /opt/forge-mk1
#   docker build -f deploy/cell-centurion.Dockerfile -t forge-cell-centurion:latest deploy/
FROM kalilinux/kali-rolling

LABEL org.opencontainers.image.title="forge-cell-centurion" \
      org.opencontainers.image.description="Headless Kali toolchain (no GUI) baked for the Forge's Centurion agent" \
      maintainer="spedatox"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# 1) The HEADLESS metapackage (Kali's CLI toolset, no desktop/X11) plus an
#    explicit curated set of the most-used tools so common jobs pay nothing, the
#    wordlist/SecLists collections, and a build toolchain for anything a job wants
#    to compile or pipx-install later. A failure here MUST break the build.
RUN apt-get update \
 && apt-get -y dist-upgrade \
 && apt-get -y install \
        kali-linux-headless \
        nmap nikto sqlmap hydra john hashcat \
        metasploit-framework exploitdb \
        nuclei ffuf gobuster feroxbuster wpscan \
        amass theharvester recon-ng \
        seclists wordlists \
        python3-pip python3-venv pipx golang-go \
        git curl wget jq unzip \
        iproute2 iputils-ping dnsutils netcat-traditional \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# 2) Decompress rockyou so it is usable out of the box (Kali ships it gzipped).
RUN if [ -f /usr/share/wordlists/rockyou.txt.gz ]; then \
        gunzip -k /usr/share/wordlists/rockyou.txt.gz; \
    fi

# 3) Warm the SearchSploit database (quick, reliable). nuclei fetches its own
#    templates on first run and Metasploit builds its module cache on first use,
#    so neither is warmed here — both wanted state a build layer can't give.
RUN searchsploit -u || true

# Cells are launched with `--workdir /workspace` and an explicit `sleep infinity`
# by the Docker Cell, so this CMD is only a sensible default for manual runs.
WORKDIR /workspace
CMD ["sleep", "infinity"]
