# Single stage: the package has no runtime dependencies, so there is nothing to
# build and nothing to copy between stages. That is the point — a tool whose
# subject is dependency risk should not arrive with a transitive tree.
FROM python:3.12-slim

# git is needed to clone the repository under audit. ca-certificates is needed
# to reach npm, OSV and the two APIs; the slim image ships it, but pinning the
# dependency explicitly documents why TLS works here.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY tools/ ./tools/

RUN pip install --no-cache-dir -e . \
 && useradd --create-home --uid 10001 agent \
 && mkdir -p /app/data /work \
 && chown -R agent:agent /app /work

# Non-root: this container holds a credential that can write to a repository.
USER agent

# Where the audited checkout lives. Mounted or cloned at run time — never baked
# into the image, so the same image audits any revision.
ENV TARGET_CHECKOUT=/work/superset \
    DB_PATH=/app/data/agent.db \
    PYTHONUNBUFFERED=1

VOLUME ["/app/data", "/work"]

# Fails without credentials, which is the correct default for a container that
# can spend money and open pull requests.
ENTRYPOINT ["remediation-agent"]
CMD ["--help"]
