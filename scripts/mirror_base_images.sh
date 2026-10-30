#!/usr/bin/env bash
# Pulls the base image(s) Launchpad's platform-owned Dockerfiles use and pushes
# them into this account's China-partition ECR, under
# Settings.base_image_mirror_prefix ("base" by default) — so CodeBuild can set
# BASE_IMAGE to an in-partition image once Settings.base_image_mirror_enabled is
# flipped on. NOT part of `make verify` (needs network + docker + the operator's
# AWS CLI profile); run manually, once per account/region, before enabling the
# flag. See docs/cn/interfaces/deploy.md.
set -Eeuo pipefail
: "${AWS_REGION:?set AWS_REGION, e.g. cn-northwest-1}"

PREFIX="${BASE_IMAGE_MIRROR_PREFIX:-base}"
# Entries must be plain `repo:tag` — no registry host/port — the `${image%%:*}`/
# `${image##*:}` split below assumes a single colon separating repo from tag.
IMAGES=("python:3.13-slim")  # keep in step with samples/byoc/hello-container/Dockerfile's ARG BASE_IMAGE default

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
PARTITION="$(aws sts get-caller-identity --query Arn --output text | cut -d: -f2)"
DNS_SUFFIX="amazonaws.com.cn"
if [ "$PARTITION" != "aws-cn" ]; then DNS_SUFFIX="amazonaws.com"; fi
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.${DNS_SUFFIX}"

aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"

for image in "${IMAGES[@]}"; do
  repo="${PREFIX}/${image%%:*}"
  tag="${image##*:}"
  aws ecr describe-repositories --repository-names "$repo" --region "$AWS_REGION" >/dev/null 2>&1 \
    || aws ecr create-repository --repository-name "$repo" --region "$AWS_REGION" >/dev/null
  docker pull --platform linux/arm64 "$image"
  docker tag "$image" "${REGISTRY}/${repo}:${tag}"
  docker push "${REGISTRY}/${repo}:${tag}"
  echo "mirrored ${image} -> ${REGISTRY}/${repo}:${tag}"
done
