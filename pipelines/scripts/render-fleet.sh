#!/usr/bin/env bash
# Render the fleet repository per environment and cluster for the merge gate:
#   <out>/<env>/<cluster>/*.yaml
# Stateless cells: infrastructure/stateless + every apps/*/stateless/<env>, with the cell's cluster-vars
# substituted the way Flux does (ENV, CLUSTER_TYPE, AZ, CLUSTER_NAME). Stateful: infrastructure/stateful + every
# apps/*/stateful/<env>. Adapt the variable values to the real cluster-vars ConfigMap.
#   usage: render-fleet.sh <fleet-checkout> <out-dir>
set -euo pipefail
src=$(cd "$1" && pwd) out=$2
mkdir -p "$out"

render() { # <env> <cluster> <type> <az>
  local env=$1 cluster=$2 type=$3 az=$4 dir="$out/$1/$2"
  mkdir -p "$dir"
  export ENV=$env CLUSTER_TYPE=$type AZ=$az CLUSTER_NAME="aks-$env-$cluster"
  [ -d "$src/infrastructure/$type" ] && kustomize build "$src/infrastructure/$type" | envsubst > "$dir/infrastructure.yaml"
  for app in "$src"/apps/*/; do
    overlay="$app$type/$env"
    [ -d "$overlay" ] && kustomize build "$overlay" | envsubst > "$dir/app-$(basename "$app").yaml"
  done
  return 0
}

for env in dev acc prd; do
  [ -d "$src/clusters/$env" ] || continue
  for cell in "$src/clusters/$env"/*/; do
    cell=$(basename "$cell")
    render "$env" "$cell" stateless "${cell##*az}"
  done
  # The stateful cluster exists only once its first workload is approved.
  if ls "$src"/apps/*/stateful/"$env" >/dev/null 2>&1; then
    render "$env" sf stateful ""
  fi
done
