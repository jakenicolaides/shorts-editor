#!/bin/bash
# Build the editor for the Lambda function and ship it. The function runs on INTEL
# (x86_64): timed 2026-11-20 it was quicker and steadier there than on ARM, at the
# same cost per take (CLAUDE.md, "The editor on Linux").
#
# Why this is a script and not `docker build`: this Mac is ARM, and the Docker here
# (the legacy builder, no buildx) IGNORES --platform and `FROM --platform=`. Twice
# that day it built an ARM image, called it done, and a function was created from
# it. So the Intel base image is pinned by its digest, which the builder cannot
# reinterpret, and the result is checked to really be Intel before anything is
# pushed. The build runs under emulation: about five minutes.
#
# Usage: bin/build-lambda.sh            build, check, push, update the function
#        bin/build-lambda.sh --no-ship  build and check only
# Needs: `aws login --region eu-west-1`, and colima (it starts and stops its own
#        profile, "editor", and leaves the default one alone).
set -euo pipefail
cd "$(dirname "$0")/.."

REGION=eu-west-1
FUNCTION=shorts-editor
REPO=shorts-editor
BASE=python:3.13-slim-trixie          # must match the Dockerfile's FROM line
DOCKER=(docker --context colima-editor)
export AWS_REGION=$REGION AWS_PAGER=""

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nFAILED: %s\n' "$*" >&2; exit 1; }

grep -q "^FROM $BASE\$" Dockerfile || die "the Dockerfile's FROM line is not '$BASE': change BASE here to match"
ACCT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) || die "not logged in: aws login --region $REGION"
REG="${ACCT}.dkr.ecr.${REGION}.amazonaws.com"

say "Starting the build machine"
colima start editor >/dev/null 2>&1 || die "colima would not start"
trap 'colima stop editor >/dev/null 2>&1 || true' EXIT

say "Finding the Intel build of $BASE"
DIGEST=$("${DOCKER[@]}" manifest inspect "$BASE" | python3 -c "
import json, sys
for m in json.load(sys.stdin).get('manifests', []):
    p = m.get('platform', {})
    if p.get('architecture') == 'amd64' and p.get('os') == 'linux':
        print(m['digest']); break")
[ -n "$DIGEST" ] || die "no linux/amd64 build of $BASE was found"
echo "$DIGEST"

say "Building (emulated, about five minutes)"
TMP=$(mktemp)
sed "s#^FROM $BASE\$#FROM ${BASE%%:*}@${DIGEST}#" Dockerfile > "$TMP"
"${DOCKER[@]}" build -f "$TMP" -t "$REPO:x86-build" . > /tmp/shorts-editor-build.log 2>&1 || { tail -20 /tmp/shorts-editor-build.log; rm -f "$TMP"; die "the build failed (log: /tmp/shorts-editor-build.log)"; }
rm -f "$TMP"

say "Checking the image really is Intel"
ARCH=$("${DOCKER[@]}" run --rm --entrypoint uname "$REPO:x86-build" -m | tail -1)
[ "$ARCH" = "x86_64" ] || die "the image is '$ARCH', not x86_64: nothing was pushed"
"${DOCKER[@]}" run --rm --entrypoint python "$REPO:x86-build" -c "import ctranslate2, faster_whisper, shorts_editor.lambda_handler; print('imports ok')"

[ "${1:-}" = "--no-ship" ] && { echo "built and checked; not shipped"; exit 0; }

say "Pushing"
aws ecr get-login-password | "${DOCKER[@]}" login --username AWS --password-stdin "$REG" >/dev/null
"${DOCKER[@]}" tag "$REPO:x86-build" "$REG/$REPO:latest"
"${DOCKER[@]}" push "$REG/$REPO:latest" > /tmp/shorts-editor-push.log 2>&1 || { tail -5 /tmp/shorts-editor-push.log; die "the push failed"; }

say "Updating the function"
aws lambda update-function-code --function-name "$FUNCTION" --image-uri "$REG/$REPO:latest" --architectures x86_64 --query LastUpdateStatus --output text
aws lambda wait function-updated-v2 --function-name "$FUNCTION"
echo "done: $FUNCTION is running the new image"
