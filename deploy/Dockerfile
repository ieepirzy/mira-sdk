# Build context is the repository root (see deploy/compose.yaml) so the image
# installs the SDK from this checkout rather than from PyPI — the running
# driver is the code in this repository, never a possibly-older release.
#
# "This checkout" used to mean the one Portainer GitOps pulled onto the host,
# because the host built the image itself. It now means CI's checkout of main:
# deploy/compose.yaml pulls ghcr.io/ieepirzy/mira-driver, which
# .github/workflows/publish.yml builds from here. The PyPI-vs-source point is
# unchanged; what moved is which machine does the install.
FROM python:3.12-slim

WORKDIR /opt/miraasdk
COPY pyproject.toml README.md LICENSE ./
COPY miraasdk ./miraasdk
RUN pip install --no-cache-dir '.[driver]'

# THIS IS CRITICAL: without it, buffered stdout can swallow the final log
# lines if the process crashes — the same rule movingfirm-backend's
# Dockerfile documents for Portainer log visibility.
ENV PYTHONUNBUFFERED=1

RUN useradd --system --no-create-home mira
USER mira

ENTRYPOINT ["mira-driver"]
