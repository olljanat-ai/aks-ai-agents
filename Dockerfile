# Agent runtime image: the agents, the merge gate, az, kubectl and kubelogin. Runs as a Container Apps job in the
# management VNet with the agent's user-assigned managed identity.
FROM python:3.12-slim-bookworm

ARG KUBECTL_VERSION=v1.34.1
ARG KUBELOGIN_VERSION=v0.2.10
ARG AZURE_CLI_VERSION=2.77.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git jq openssl unzip gnupg lsb-release \
 && mkdir -p /etc/apt/keyrings \
 && curl -sLS https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /etc/apt/keyrings/microsoft.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/azure-cli/ $(lsb_release -cs) main" \
      > /etc/apt/sources.list.d/azure-cli.list \
 && apt-get update && apt-get install -y --no-install-recommends "azure-cli=${AZURE_CLI_VERSION}-1~$(lsb_release -cs)" \
 && az extension add --name resource-graph --only-show-errors \
 && az extension add --name costmanagement --only-show-errors \
 && ARCH=$(dpkg --print-architecture) \
 && curl -sLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl" \
 && chmod +x /usr/local/bin/kubectl \
 && curl -sLo /tmp/kubelogin.zip "https://github.com/Azure/kubelogin/releases/download/${KUBELOGIN_VERSION}/kubelogin-linux-${ARCH}.zip" \
 && unzip -j /tmp/kubelogin.zip -d /usr/local/bin && rm /tmp/kubelogin.zip \
 && apt-get purge -y gnupg lsb-release unzip && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/aks-ai-agents
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . azure-identity
COPY agents ./agents
COPY guardrails ./guardrails
COPY config ./config
COPY deploy/entrypoint.sh /usr/local/bin/agent-entrypoint

RUN useradd --create-home --uid 10001 agent && mkdir -p /work && chown agent /work
USER agent
ENV AKS_AGENT_ROOT=/opt/aks-ai-agents \
    AKS_AGENT_WORKSPACE=/work/workspace \
    AKS_AGENT_RUNS=/work/runs \
    AKS_AGENT_DESIGN=/work/onek8s.github.io \
    KUBECONFIG=/work/kubeconfig
ENTRYPOINT ["agent-entrypoint"]
