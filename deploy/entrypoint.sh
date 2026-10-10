#!/usr/bin/env bash
# Prepares one agent run and starts it. Everything here runs as the agent's own managed identity:
#   1. Azure login with the user-assigned managed identity (no secrets);
#   2. a short-lived GitHub App installation token minted from the agent's App key (Key Vault in sub-mgmt);
#   3. the design repository (the specification) and the guardrails as data;
#   4. a kubeconfig with one context per cluster of the environment (Entra ID, kubelogin, no admin credentials).
#
# Inputs (Container Apps job environment):
#   AKS_AGENT_NAME, AKS_AGENT_ENV, AZURE_CLIENT_ID, AKS_AGENT_GITHUB_APP_ID, AKS_AGENT_GITHUB_INSTALLATION_ID,
#   AKS_AGENT_GITHUB_KEY_VAULT, AKS_AGENT_GITHUB_KEY_SECRET, AKS_AGENT_TASK, optional AKS_AGENT_TRIGGER_JSON,
#   AKS_AGENT_MODE, AKS_AGENT_DRY_RUN
set -euo pipefail

: "${AKS_AGENT_NAME:?}" "${AKS_AGENT_ENV:?}" "${AZURE_CLIENT_ID:?}" "${AKS_AGENT_TASK:?}"
cd /work

az login --identity --client-id "$AZURE_CLIENT_ID" --allow-no-subscriptions --only-show-errors >/dev/null

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

github_token() {
  local key now header payload unsigned signature jwt
  key=$(mktemp)
  az keyvault secret show --vault-name "$AKS_AGENT_GITHUB_KEY_VAULT" --name "$AKS_AGENT_GITHUB_KEY_SECRET" \
    --query value -o tsv > "$key"
  now=$(date +%s)
  header=$(printf '{"alg":"RS256","typ":"JWT"}' | b64url)
  payload=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' $((now - 60)) $((now + 540)) "$AKS_AGENT_GITHUB_APP_ID" | b64url)
  unsigned="$header.$payload"
  signature=$(printf '%s' "$unsigned" | openssl dgst -sha256 -sign "$key" | b64url)
  rm -f "$key"
  jwt="$unsigned.$signature"
  curl -fsS -X POST -H "Authorization: Bearer $jwt" -H "Accept: application/vnd.github+json" \
    "https://api.github.com/app/installations/$AKS_AGENT_GITHUB_INSTALLATION_ID/access_tokens" | jq -r .token
}

if [ -n "${AKS_AGENT_GITHUB_APP_ID:-}" ]; then
  AKS_AGENT_GITHUB_TOKEN=$(github_token)
  export AKS_AGENT_GITHUB_TOKEN
fi

design_repo=$(python -c 'import yaml,sys; print(yaml.safe_load(open(sys.argv[1]))["github"]["repositories"]["design"])' \
  "$AKS_AGENT_ROOT/config/platform.yaml")
auth=()
if [ -n "${AKS_AGENT_GITHUB_TOKEN:-}" ]; then
  auth=(-c "http.extraHeader=Authorization: Basic $(printf 'x-access-token:%s' "$AKS_AGENT_GITHUB_TOKEN" | base64 -w0)")
fi
git "${auth[@]}" clone --quiet --depth 1 "https://github.com/$design_repo.git" "$AKS_AGENT_DESIGN"

# One kubeconfig context per cluster of the environment (user credentials only, converted to managed identity).
clusters=$(python - "$AKS_AGENT_ROOT/config/platform.yaml" "$AKS_AGENT_ENV" <<'PY'
import sys, yaml
env = yaml.safe_load(open(sys.argv[1]))["environments"][sys.argv[2]]
for c in env.get("clusters", []):
    print(env["subscription"], env["resourceGroup"], c)
PY
)
while read -r subscription group cluster; do
  [ -n "$cluster" ] || continue
  az aks get-credentials --subscription "$subscription" -g "$group" -n "$cluster" --context "$cluster" \
    --file "$KUBECONFIG" --overwrite-existing --only-show-errors || echo "warning: no credentials for $cluster" >&2
done <<< "$clusters"
[ -f "$KUBECONFIG" ] && kubelogin convert-kubeconfig -l msi --client-id "$AZURE_CLIENT_ID"

trigger=()
if [ -n "${AKS_AGENT_TRIGGER_JSON:-}" ]; then
  printf '%s' "$AKS_AGENT_TRIGGER_JSON" > /work/trigger.json
  trigger=(--trigger /work/trigger.json)
fi

exec aks-agent run "$AKS_AGENT_NAME" --env "$AKS_AGENT_ENV" --task "$AKS_AGENT_TASK" "${trigger[@]}"
