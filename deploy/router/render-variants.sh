#!/usr/bin/env bash
# Renders deploy/router/variants/<name>.yaml: epp.yaml with each yq expression
# from variants.yaml applied. The rendered files are committed; CI reruns this
# and fails if they differ. Needs yq v4 (github.com/mikefarah/yq).
set -euo pipefail
cd "$(dirname "$0")"
for v in $(yq -r 'keys | .[]' variants.yaml); do
  { echo "# Generated from epp.yaml + variants.yaml[$v] by render-variants.sh. Do not edit."
    yq "$(yq -r ".\"$v\"" variants.yaml)" epp.yaml; } > "variants/$v.yaml"
done
