# syntax=docker/dockerfile:1.6
ARG ODOO_SOURCE_REPOSITORY=https://github.com/odoo/odoo.git
ARG ODOO_SOURCE_REF=20.0
ARG ODOO_SOURCE_REV
ARG PYTHON_VERSION
ARG APT_SNAPSHOT
ARG POSTGRESQL_CLIENT_VERSION
ARG POSTGRESQL_CLIENT_COMMON_VERSION
ARG POSTGRESQL_LIBPQ_VERSION
ARG RESOLVER_CUTOFF
ARG NATIVE_BUILD_PACKAGES="build-essential libcairo2-dev libjpeg-dev libldap2-dev libsasl2-dev libssl-dev libxml2-dev libxslt1-dev pkg-config zlib1g-dev"

# Keep the official uv image first so Renovate tracks it for Docker updates.
FROM --platform=$TARGETPLATFORM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv-binary

FROM --platform=$BUILDPLATFORM alpine/git:v2.54.0@sha256:6a1fdb962814ca11508ca8d1bab94375b5b7ac4e4a9a40981c29fc32c02f095d AS odoo-source
ARG ODOO_SOURCE_REPOSITORY
ARG ODOO_SOURCE_REF
ARG ODOO_SOURCE_REV
WORKDIR /source
RUN set -eux; \
    git init odoo; \
    git -C odoo remote add origin "${ODOO_SOURCE_REPOSITORY}"; \
    if [ -n "${ODOO_SOURCE_REV:-}" ]; then \
      git -C odoo fetch --depth 1 origin "${ODOO_SOURCE_REV}"; \
    else \
      git -C odoo fetch --depth 1 origin "refs/heads/${ODOO_SOURCE_REF}"; \
    fi; \
    git -C odoo checkout --detach FETCH_HEAD; \
    rm -rf odoo/.git
COPY patches /patches
RUN if grep -Eq '^version_info = \(20,' odoo/odoo/release.py; then \
      git -C odoo apply /patches/20.0-valuation-checkpoints.patch; \
      git -C odoo apply /patches/20.0-inline-parent-context.patch; \
      git -C odoo apply /patches/20.0-pdf-media-box.patch; \
    fi

# Community addons for 20.0 on the image-owned addons path. No module here auto-installs.
# muk_web_enterprise_theme needs web_enterprise and is never copied.
FROM --platform=$BUILDPLATFORM alpine/git:v2.54.0@sha256:6a1fdb962814ca11508ca8d1bab94375b5b7ac4e4a9a40981c29fc32c02f095d AS community-addons
ARG ODOO_SOURCE_REF
ARG MUK_MODULES_REPOSITORY=https://github.com/muk-it/odoo-modules.git
ARG MUK_MODULES_REV=2191e73ed8150f3e01ca235db679f962efa3a087
ARG MUK_THEME_MODULES="muk_web_theme muk_web_utils muk_web_group muk_web_chatter muk_web_dialog muk_web_appsbar muk_web_colors muk_web_refresh muk_web_list_column muk_web_list_mode"
ARG OCA_KNOWLEDGE_REPOSITORY=https://github.com/OCA/knowledge.git
ARG OCA_KNOWLEDGE_REV=87b058922c0cf980ddfb4b6341f3de805a3ad476
ARG OCA_KNOWLEDGE_MODULES="document_page document_knowledge"
ARG OCA_PRODUCT_ATTRIBUTE_REPOSITORY=https://github.com/OCA/product-attribute.git
ARG OCA_PRODUCT_ATTRIBUTE_REV=b0aa90757aea6057f764fb0938550b76a465a0a1
WORKDIR /source
COPY patches/20.0-muk-chatter-layout.patch patches/20.0-muk-appsbar-toggle.patch /patches/
RUN set -eux; \
    mkdir addons; \
    copy_pinned_modules() { \
      repository="$1"; revision="$2"; license="$3"; modules="$4"; \
      git init checkout; \
      git -C checkout remote add origin "${repository}"; \
      git -C checkout fetch --depth 1 origin "${revision}"; \
      git -C checkout checkout --detach FETCH_HEAD; \
      for module in ${modules}; do \
        grep -Eq "[\"']license[\"']: *[\"']${license}[\"']" "checkout/${module}/__manifest__.py"; \
        cp -R "checkout/${module}" addons/; \
      done; \
      rm -rf checkout; \
    }; \
    if [ "${ODOO_SOURCE_REF}" = "20.0" ]; then \
      copy_pinned_modules "${MUK_MODULES_REPOSITORY}" "${MUK_MODULES_REV}" LGPL-3 "${MUK_THEME_MODULES}"; \
      git -C addons apply /patches/20.0-muk-chatter-layout.patch /patches/20.0-muk-appsbar-toggle.patch; \
      copy_pinned_modules "${OCA_KNOWLEDGE_REPOSITORY}" "${OCA_KNOWLEDGE_REV}" AGPL-3 "${OCA_KNOWLEDGE_MODULES}"; \
      copy_pinned_modules "${OCA_PRODUCT_ATTRIBUTE_REPOSITORY}" "${OCA_PRODUCT_ATTRIBUTE_REV}" AGPL-3 product_sequence; \
    fi

FROM --platform=$BUILDPLATFORM alpine/curl:8.22.0@sha256:f05779b04a6f3c6f228c67118786f5c80d4c4922dd79b0b24c92dc10a74f594f AS wkhtmltox
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

FROM ubuntu:noble@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852 AS runtime-system
ARG PYTHON_VERSION
ARG ODOO_SOURCE_REF
ARG APT_REFRESH_EPOCH=0
ARG APT_SNAPSHOT
ARG POSTGRESQL_CLIENT_VERSION
ARG POSTGRESQL_CLIENT_COMMON_VERSION
ARG POSTGRESQL_LIBPQ_VERSION
ENV DEBIAN_FRONTEND=noninteractive
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

COPY --from=wkhtmltox /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt

RUN set -eux; \
    sed -i \
      -e 's|http:/[/]archive.ubuntu.com/ubuntu/|https://archive.ubuntu.com/ubuntu/|g' \
      -e 's|http:/[/]security.ubuntu.com/ubuntu/|https://security.ubuntu.com/ubuntu/|g' \
      -e 's|http:/[/]ports.ubuntu.com/ubuntu-ports/|https://ports.ubuntu.com/ubuntu-ports/|g' \
      /etc/apt/sources.list.d/ubuntu.sources; \
    printf '%s\n' \
      'Acquire::Retries "5";' \
      'Acquire::http::Timeout "30";' \
      'Acquire::https::Timeout "30";' \
      'APT::Update::Error-Mode "any";' \
      > /etc/apt/apt.conf.d/80odoo-network-hardening; \
    if [ -n "${APT_SNAPSHOT:-}" ]; then \
      snapshot_url="https://snapshot.ubuntu.com/ubuntu/${APT_SNAPSHOT}/"; \
      sed -i \
        -e "s|https://archive.ubuntu.com/ubuntu/|${snapshot_url}|g" \
        -e "s|https://security.ubuntu.com/ubuntu/|${snapshot_url}|g" \
        -e "s|https://ports.ubuntu.com/ubuntu-ports/|${snapshot_url}|g" \
        /etc/apt/sources.list.d/ubuntu.sources; \
    fi

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    echo "apt refresh epoch: ${APT_REFRESH_EPOCH}" \
    && apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      fontconfig \
      fonts-noto-cjk \
      gettext \
      git \
      libjpeg-turbo8 \
      libldap2 \
      libsasl2-2 \
      libssl3t64 \
      libx11-6 \
      libxcb1 \
      libcairo2 \
      libxext6 \
      libxml2 \
      libxslt1.1 \
      libxrender1 \
      node-less \
      openssh-client \
      python3 \
      rsync \
      tini \
      xfonts-75dpi \
      xfonts-base \
      xz-utils \
      zlib1g \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    -o /usr/share/keyrings/postgresql.asc \
    && echo "deb [signed-by=/usr/share/keyrings/postgresql.asc] https://apt-archive.postgresql.org/pub/repos/apt noble-pgdg-archive main" \
      > /etc/apt/sources.list.d/pgdg.list

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
    && if [ -n "${POSTGRESQL_CLIENT_VERSION:-}" ] \
      && [ -n "${POSTGRESQL_CLIENT_COMMON_VERSION:-}" ] \
      && [ -n "${POSTGRESQL_LIBPQ_VERSION:-}" ]; then \
      apt-get install -y --no-install-recommends \
        "postgresql-client-17=${POSTGRESQL_CLIENT_VERSION}" \
        "postgresql-client-common=${POSTGRESQL_CLIENT_COMMON_VERSION}" \
        "libpq5=${POSTGRESQL_LIBPQ_VERSION}"; \
    else \
      apt-get install -y --no-install-recommends postgresql-client-17; \
    fi \
    && rm -rf /var/lib/apt/lists/*

COPY --from=wkhtmltox /wkhtmltox/wkhtmltox.deb /tmp/wkhtmltox.deb
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends /tmp/wkhtmltox.deb \
    && rm -f /tmp/wkhtmltox.deb \
    && rm -rf /var/lib/apt/lists/*

RUN if ! id -u ubuntu >/dev/null 2>&1; then useradd --create-home --shell /bin/bash ubuntu; fi

COPY --from=uv-binary /uv /uvx /usr/local/bin/
RUN install -d -o ubuntu -g ubuntu /odoo
COPY --from=odoo-source --chown=ubuntu:ubuntu /source/odoo/requirements.txt /odoo/requirements.txt
COPY requirements-overrides.txt /odoo/requirements-overrides.txt
COPY image-variants.json /odoo/image-variants.json

ENV PATH="/venv/bin:/usr/local/bin:${PATH}"
ENV VIRTUAL_ENV=/venv
ENV UV_CACHE_DIR=/home/ubuntu/.cache/uv
ENV UV_PROJECT_ENVIRONMENT=/venv
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python

FROM runtime-system AS runtime-pythondeps
ARG RESOLVER_CUTOFF
ARG NATIVE_BUILD_PACKAGES

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    --mount=type=cache,target=/root/.npm,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends \
      ${NATIVE_BUILD_PACKAGES} "libpq-dev=$(dpkg-query -W -f='${Version}' libpq5)" npm \
    && if [ -n "${RESOLVER_CUTOFF:-}" ]; then \
      npm install --global --before="${RESOLVER_CUTOFF}" rtlcss@4.3.0; \
    else \
      npm install --global rtlcss@4.3.0; \
    fi \
    && rm -rf /var/lib/apt/lists/*

RUN --mount=type=cache,target=/home/ubuntu/.cache/uv,uid=1000,gid=1000,sharing=locked \
    install -d -o ubuntu -g ubuntu /opt/uv/python /venv /home/ubuntu/.cache/uv \
    && chown -R ubuntu:ubuntu /home/ubuntu/.cache/uv \
    && python_version="${PYTHON_VERSION:-$(python3 -c 'import json, sys; print(next(variant["python_version"] for variant in json.load(open("/odoo/image-variants.json")) if variant["series"] == sys.argv[1]))' "${ODOO_SOURCE_REF}")}" \
    && su -s /bin/bash ubuntu -c "uv python install '${python_version}'" \
    && su -s /bin/bash ubuntu -c "uv venv /venv --python '${python_version}'" \
    && base_python="$(su -s /bin/bash ubuntu -c 'PYTHONDONTWRITEBYTECODE=1 /venv/bin/python -c "import sys; print(sys._base_executable)"')" \
    && case "${base_python}" in /opt/uv/python/*) ;; *) echo "Unexpected base interpreter: ${base_python}" >&2; exit 1 ;; esac \
    && base_site_packages="$(env -u VIRTUAL_ENV PYTHONDONTWRITEBYTECODE=1 "${base_python}" -c 'import sysconfig; print(sysconfig.get_path("purelib"))')" \
    && base_scripts="$(env -u VIRTUAL_ENV PYTHONDONTWRITEBYTECODE=1 "${base_python}" -c 'import sysconfig; print(sysconfig.get_path("scripts"))')" \
    && base_stdlib="$(env -u VIRTUAL_ENV PYTHONDONTWRITEBYTECODE=1 "${base_python}" -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')" \
    && for managed_path in "${base_site_packages}" "${base_scripts}" "${base_stdlib}"; do \
      case "${managed_path}" in /opt/uv/python/*) ;; *) echo "Unexpected managed Python path: ${managed_path}" >&2; exit 1 ;; esac; \
      test -d "${managed_path}"; \
    done \
    && rm -rf \
      "${base_site_packages}/pip" \
      "${base_site_packages}"/pip-*.dist-info \
      "${base_scripts}"/pip* \
      "${base_stdlib}/ensurepip" \
    && test -z "$(find "${base_site_packages}" -maxdepth 1 -name 'pip-*.dist-info' -print -quit)" \
    && test -z "$(find "${base_scripts}" -maxdepth 1 -name 'pip*' -print -quit)" \
    && env -u VIRTUAL_ENV PYTHONDONTWRITEBYTECODE=1 "${base_python}" -c 'import importlib.util; assert importlib.util.find_spec("pip") is None; assert importlib.util.find_spec("ensurepip") is None' \
    && if [ -n "${RESOLVER_CUTOFF:-}" ]; then export UV_EXCLUDE_NEWER="${RESOLVER_CUTOFF}"; fi \
    && case "${python_version}" in 3.14.*) export UV_OVERRIDE=/odoo/requirements-overrides.txt ;; esac \
    && su -s /bin/bash ubuntu -c "uv pip install --python /venv/bin/python -r /odoo/requirements.txt" \
    && su -s /bin/bash ubuntu -c "uv pip install --python /venv/bin/python -r /odoo/requirements-overrides.txt" \
    && su -s /bin/bash ubuntu -c "uv pip install --python /venv/bin/python rlpycairo==0.4.0" \
    && su -s /bin/bash ubuntu -c "uv pip check --python /venv/bin/python"

FROM runtime-system AS runtime

COPY --from=runtime-pythondeps --chown=ubuntu:ubuntu /opt/uv/python/ /opt/uv/python/
COPY --from=runtime-pythondeps --chown=ubuntu:ubuntu /venv/ /venv/
COPY --from=runtime-pythondeps /usr/local/lib/node_modules/rtlcss/ /usr/local/lib/node_modules/rtlcss/
COPY --from=odoo-source --chown=ubuntu:ubuntu /source/odoo /odoo
COPY --chown=ubuntu:ubuntu launchplane/addons /opt/launchplane/addons
COPY --from=community-addons --chown=ubuntu:ubuntu /source/addons/ /opt/launchplane/addons/
COPY scripts/odoo-bin-wrapper.sh /usr/local/bin/odoo-bin-wrapper.sh
COPY scripts/configure-dev-addon-paths.sh /usr/local/bin/configure-dev-addon-paths.sh
COPY scripts/odoo-python-sync.sh /usr/local/bin/odoo-python-sync.sh
COPY scripts/odoo-python-sync.py /usr/local/lib/odoo-python-sync.py
COPY scripts/odoo-fetch-addons.sh /usr/local/bin/odoo-fetch-addons.sh
COPY --chmod=0755 scripts/launchplane-readiness.py /usr/local/bin/launchplane-readiness

RUN /venv/bin/python /usr/local/lib/odoo-python-sync.py export-base-constraints \
      > /opt/launchplane/base-python-constraints.txt \
    && chmod 0644 /opt/launchplane/base-python-constraints.txt

RUN rm /etc/apt/sources.list.d/pgdg.list \
    && ln -s ../lib/node_modules/rtlcss/bin/rtlcss.js /usr/local/bin/rtlcss \
    && mv /odoo/odoo-bin /odoo/odoo-bin.source \
    && install -m 0755 /usr/local/bin/odoo-bin-wrapper.sh /odoo/odoo-bin \
    && ln -sfn /odoo/odoo-bin.source /usr/local/bin/odoo-source-bin \
    && ln -sfn /odoo/odoo-bin /usr/local/bin/odoo-bin \
    && ln -sfn /odoo/odoo-bin /usr/local/bin/odoo \
    && chmod +x /usr/local/bin/odoo-python-sync.sh /usr/local/bin/odoo-fetch-addons.sh \
    && mkdir -p /usr/lib/python3/dist-packages/addons

# Remove duplicate source/build trees that confuse IDE/module indexing.
RUN rm -rf /odoo/build/lib

RUN install -d -o ubuntu -g ubuntu /opt/runtime /opt/project /opt/project/addons /opt/extra_addons /opt/launchplane/addons /opt/launchplane/evidence /volumes/addons /volumes/config /volumes/data /volumes/logs \
    && install -o ubuntu -g ubuntu -m 0644 /dev/null /volumes/config/_generated.conf \
    && su -s /bin/bash ubuntu -c "printf '[options]\n' > /volumes/config/_generated.conf"

RUN ln -sf /etc/ssl/certs/ca-certificates.crt /usr/lib/ssl/cert.pem
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ENV ODOO_RC=/volumes/config/_generated.conf
ENV ODOO_ADDONS_PATH=/opt/project/addons,/opt/extra_addons,/opt/launchplane/addons,/odoo/addons,/odoo/odoo/addons
ENV ODOO_SERVER_WIDE_MODULES=base,web,launchplane_runtime_health
ENV ODOO_DATA_DIR=/volumes/data

WORKDIR /volumes
USER ubuntu

FROM runtime AS runtime-devtools
USER root
ARG PLAYWRIGHT_VERSION=1.59.1
ARG RESOLVER_CUTOFF
ARG NATIVE_BUILD_PACKAGES

RUN chmod +x /usr/local/bin/configure-dev-addon-paths.sh \
    && /usr/local/bin/configure-dev-addon-paths.sh

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    --mount=type=cache,target=/root/.npm,sharing=locked \
    --mount=from=runtime-system,source=/etc/apt/sources.list.d/pgdg.list,target=/etc/apt/sources.list.d/pgdg.list \
    apt-get update \
    && apt-get install -y --no-install-recommends \
      ${NATIVE_BUILD_PACKAGES} "libpq-dev=$(dpkg-query -W -f='${Version}' libpq5)" \
      fonts-liberation \
      libasound2t64 \
      libatk-bridge2.0-0t64 \
      libatk1.0-0t64 \
      libatspi2.0-0t64 \
      libcups2t64 \
      libdbus-1-3 \
      libgbm1 \
      libglib2.0-0t64 \
      libnspr4 \
      libnss3 \
      libpango-1.0-0 \
      libu2f-udev \
      libxcomposite1 \
      libxdamage1 \
      libxfixes3 \
      libxkbcommon0 \
      libxrandr2 \
      npm \
      ripgrep \
    && if [ -n "${RESOLVER_CUTOFF:-}" ]; then \
      PLAYWRIGHT_BROWSERS_PATH=/ms-playwright npx --yes --before="${RESOLVER_CUTOFF}" "playwright@${PLAYWRIGHT_VERSION}" install chromium --no-shell; \
    else \
      PLAYWRIGHT_BROWSERS_PATH=/ms-playwright npx --yes "playwright@${PLAYWRIGHT_VERSION}" install chromium --no-shell; \
    fi \
    && chromium_path="$(find /ms-playwright -path '*/chrome-linux*/chrome' -type f | sort | head -n 1)" \
    && test -x "${chromium_path}" \
    && ln -sfn "${chromium_path}" /usr/local/bin/chromium-playwright \
    && /usr/local/bin/chromium-playwright --version \
    && rm -rf /var/lib/apt/lists/*

ENV CHROME_BIN=/usr/local/bin/chromium-playwright
USER ubuntu
