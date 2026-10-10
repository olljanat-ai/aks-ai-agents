#!/usr/bin/env bash
# Terraform plan of one environment as JSON, for the merge gate: <out>/plan.json
# Runs with the environment's read-only *plan* identity (OIDC). The deploy identity accepts only tokens of the
# protected main branch, so a pull request branch – an agent's or a human's – can plan but never apply.
#   usage: terraform-plan.sh <env> <out-dir>
set -euo pipefail
env=$1 out=$2
mkdir -p "$out"
cd "environments/$env"
terraform init -input=false -lock=false
terraform plan -input=false -lock=false -out=tfplan
terraform show -json tfplan > "$out/plan.json"
terraform show -no-color tfplan > "$out/plan.txt"   # human-readable copy for reviewers
