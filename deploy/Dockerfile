# Build context is the repository root (see deploy/compose.yaml) so the
# image installs the SDK from this checkout — the running driver is always
# the code in the branch Portainer GitOps pulled, never a possibly-older
# PyPI release.
FROM python:3.12-slim

WORKDIR /opt/mira-sdk
COPY pyproject.toml README.md LICENSE ./
COPY mira_sdk ./mira_sdk
RUN pip install --no-cache-dir '.[driver]'

# THIS IS CRITICAL: without it, buffered stdout can swallow the final log
# lines if the process crashes — the same rule movingfirm-backend's
# Dockerfile documents for Portainer log visibility.
ENV PYTHONUNBUFFERED=1

RUN useradd --system --no-create-home mira
USER mira

ENTRYPOINT ["mira-driver"]
