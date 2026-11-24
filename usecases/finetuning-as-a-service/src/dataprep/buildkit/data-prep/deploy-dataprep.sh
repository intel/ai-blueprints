#!/bin/bash
# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

set -e

# Get the absolute path of the dataprep directory
# buildkit/data-prep is 3 levels deep from data-prep-backend root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_CONTEXT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
export BUILD_CONTEXT
export REGISTRY_URL="${REGISTRY_URL:-registry.kube-system.svc.cluster.local:5000}"

echo "Building with context: $BUILD_CONTEXT"
echo "Registry: $REGISTRY_URL"



# Delete old job if exists
kubectl delete job buildkit-data-prep-backend -n dataprep --ignore-not-found=true

# Pass proxy settings into the build if set in the environment
export http_proxy="${http_proxy:-}"
export https_proxy="${https_proxy:-}"
export no_proxy="${no_proxy:-}"

# Apply the job with substituted values using envsubst
envsubst < "$SCRIPT_DIR/buildkit-job.yaml" | kubectl apply -f -

echo "BuildKit job created successfully"
echo "Monitor with: kubectl logs -f job/buildkit-data-prep-backend -n dataprep"
