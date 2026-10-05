ARG BASE_IMAGE=almalinux:9.8
FROM ${BASE_IMAGE}
RUN if command -v apt-get >/dev/null; then \
      apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates \
      && rm -rf /var/lib/apt/lists/*; \
    else \
      dnf install -y python3 ca-certificates && dnf clean all; \
    fi

