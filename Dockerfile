# syntax=docker/dockerfile:1.6
ARG ODOO_SOURCE_REPOSITORY=https://github.com/odoo/odoo.git
ARG ODOO_SOURCE_REF=19.0
ARG ODOO_SOURCE_REV
ARG PYTHON_VERSION=3.13
ARG BUILDPLATFORM
ARG TARGETPLATFORM

# Keep the official uv image first so Dependabot tracks it for Docker updates.
FROM --platform=$TARGETPLATFORM ghcr.io/astral-sh/uv:0.10.8@sha256:88234bc9e09c2b2f6d176a3daf411419eb0370d450a08129257410de9cfafd2a AS uv-binary

FROM --platform=$BUILDPLATFORM alpine/git:2.49.1 AS odoo-source
ARG ODOO_SOURCE_REPOSITORY
ARG ODOO_SOURCE_REF
ARG ODOO_SOURCE_REV
WORKDIR /source
RUN set -eux; \
    git init odoo; \
    cd odoo; \
    git remote add origin "${ODOO_SOURCE_REPOSITORY}"; \
    if [ -n "${ODOO_SOURCE_REV}" ]; then \
      git fetch --depth 1 origin "${ODOO_SOURCE_REV}"; \
    else \
      git fetch --depth 1 origin "refs/heads/${ODOO_SOURCE_REF}"; \
    fi; \
    git checkout --detach FETCH_HEAD

FROM --platform=$BUILDPLATFORM alpine/curl:8.12.1 AS wkhtmltox
ARG TARGETARCH
ARG WKHTMLTOPDF_VERSION=0.12.6.1-3
ARG WKHTMLTOPDF_TARGET=jammy
ARG WKHTMLTOPDF_AMD64_SHA=967390a759707337b46d1c02452e2bb6b2dc6d59
ARG WKHTMLTOPDF_ARM64_SHA=90f6e69896d51ef77339d3f3a20f8582bdf496cc
ARG WKHTMLTOPDF_PPC64EL_SHA=5312d7d34a25b321282929df82e3574319aed25c
WORKDIR /wkhtmltox
RUN set -eux; \
    arch="${TARGETARCH}"; \
    if [ -z "${arch}" ]; then arch="$(uname -m)"; fi; \
    case "${arch}" in \
      amd64|x86_64) package_arch="amd64"; checksum="${WKHTMLTOPDF_AMD64_SHA}" ;; \
      arm64|aarch64) package_arch="arm64"; checksum="${WKHTMLTOPDF_ARM64_SHA}" ;; \
      ppc64le|ppc64el) package_arch="ppc64el"; checksum="${WKHTMLTOPDF_PPC64EL_SHA}" ;; \
      *) echo "Unsupported architecture: ${arch}" >&2; exit 1 ;; \
    esac; \
    curl -fSL --retry 5 --retry-all-errors --connect-timeout 30 \
      -o wkhtmltox.deb \
      "https://github.com/wkhtmltopdf/packaging/releases/download/${WKHTMLTOPDF_VERSION}/wkhtmltox_${WKHTMLTOPDF_VERSION}.${WKHTMLTOPDF_TARGET}_${package_arch}.deb"; \
    echo "${checksum}  wkhtmltox.deb" | sha1sum -c -

FROM ubuntu:noble AS runtime
ARG PYTHON_VERSION
ENV DEBIAN_FRONTEND=noninteractive
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends \
      build-essential \
      ca-certificates \
      curl \
      dirmngr \
      fontconfig \
      fonts-noto-cjk \
      gettext \
      git \
      gnupg \
      libjpeg-dev \
      libldap2-dev \
      libpq-dev \
      libsasl2-dev \
      libssl-dev \
      libx11-6 \
      libxcb1 \
      libcairo2 \
      libcairo2-dev \
      libxext6 \
      libxml2-dev \
      libxslt1-dev \
      libxrender1 \
      node-less \
      npm \
      openssh-client \
      pkg-config \
      python3 \
      python3-venv \
      ripgrep \
      rsync \
      tini \
      xfonts-75dpi \
      xfonts-base \
      xz-utils \
      zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    | gpg --dearmor -o /usr/share/keyrings/postgresql.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/postgresql.gpg] https://apt.postgresql.org/pub/repos/apt noble-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-17 \
    && rm -f /etc/apt/sources.list.d/pgdg.list \
    && rm -rf /var/lib/apt/lists/*

COPY --from=wkhtmltox /wkhtmltox/wkhtmltox.deb /tmp/wkhtmltox.deb
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends /tmp/wkhtmltox.deb \
    && rm -f /tmp/wkhtmltox.deb \
    && rm -rf /var/lib/apt/lists/*

RUN npm install --global rtlcss@4.3.0

RUN if ! id -u ubuntu >/dev/null 2>&1; then useradd --create-home --shell /bin/bash ubuntu; fi

COPY --from=odoo-source --chown=ubuntu:ubuntu /source/odoo /odoo
COPY --from=uv-binary /uv /uvx /usr/local/bin/
COPY scripts/odoo-bin-wrapper.sh /usr/local/bin/odoo-bin-wrapper.sh

ENV PATH="/venv/bin:/usr/local/bin:${PATH}"
ENV VIRTUAL_ENV=/venv
ENV UV_CACHE_DIR=/home/ubuntu/.cache/uv
ENV UV_PROJECT_ENVIRONMENT=/venv
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python

RUN install -d -o ubuntu -g ubuntu /opt/uv/python /venv /home/ubuntu/.cache/uv \
    && su -s /bin/bash ubuntu -c "uv python install '${PYTHON_VERSION}'" \
    && su -s /bin/bash ubuntu -c "uv venv /venv --python '${PYTHON_VERSION}'" \
    && su -s /bin/bash ubuntu -c "uv pip install --python /venv/bin/python --upgrade pip" \
    && su -s /bin/bash ubuntu -c "uv pip install --python /venv/bin/python -r /odoo/requirements.txt" \
    && su -s /bin/bash ubuntu -c "uv pip install --python /venv/bin/python /odoo" \
    && su -s /bin/bash ubuntu -c "uv pip install --python /venv/bin/python rlpycairo"

RUN mv /odoo/odoo-bin /usr/local/bin/odoo-source-bin \
    && install -m 0755 /usr/local/bin/odoo-bin-wrapper.sh /odoo/odoo-bin \
    && ln -sfn /odoo/odoo-bin /usr/local/bin/odoo-bin \
    && ln -sfn /venv/bin/odoo /usr/local/bin/odoo \
    && mkdir -p /usr/lib/python3/dist-packages/addons

RUN install -d -o ubuntu -g ubuntu /opt/project /opt/extra_addons /volumes/addons /volumes/config /volumes/data /volumes/logs \
    && install -o ubuntu -g ubuntu -m 0644 /dev/null /volumes/config/_generated.conf \
    && su -s /bin/bash ubuntu -c "printf '[options]\n' > /volumes/config/_generated.conf"

RUN ln -sf /etc/ssl/certs/ca-certificates.crt /usr/lib/ssl/cert.pem
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ENV ODOO_RC=/volumes/config/_generated.conf
ENV ODOO_ADDONS_PATH=/opt/project/addons,/opt/extra_addons,/odoo/addons,/odoo/odoo/addons
ENV ODOO_DATA_DIR=/volumes/data

WORKDIR /volumes
USER ubuntu

FROM runtime AS runtime-devtools
USER root
ARG UBUNTU_CODENAME=noble

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && mkdir -p /usr/share/keyrings \
    && curl -fsSL "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x82BB6851C64F6880" \
      | gpg --dearmor -o /usr/share/keyrings/xtradeb-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/xtradeb-archive-keyring.gpg] https://ppa.launchpadcontent.net/xtradeb/apps/ubuntu ${UBUNTU_CODENAME} main" \
      > /etc/apt/sources.list.d/xtradeb-apps.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends chromium fonts-liberation libu2f-udev \
    && rm -f /etc/apt/sources.list.d/xtradeb-apps.list \
    && rm -rf /var/lib/apt/lists/*

ENV CHROME_BIN=/usr/bin/chromium
USER ubuntu
