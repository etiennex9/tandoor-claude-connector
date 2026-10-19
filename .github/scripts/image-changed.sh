#!/usr/bin/env bash
# Prints whether publish.yml should build the gateway image: true or false.
#
# The image is built when one of its inputs changed in the given commit range. Its inputs are read from its Dockerfile:
# the Dockerfile itself and what it copies from the build context (gateway/). It's always built when there's no range,
# when the range can't be compared (rewritten history), or when the publishing workflow or one of its scripts in
# .github/scripts/ changed, such as this one or the smoke test.
#
# Usage: image-changed.sh [<range>], e.g. image-changed.sh 8bbc50e..HEAD
set -e
cd "$(git rev-parse --show-toplevel)"

dockerfile=gateway/Dockerfile
range=$1
build=false
if [ -z "$range" ] || ! changed=$(git diff --name-only "$range" 2>/dev/null); then
    build=true
elif grep -qE '^\.github/(workflows/publish\.yml|scripts/.+)$' <<<"$changed"; then
    build=true
else
    # Path prefixes as a regex: the Dockerfile, then each COPY source taken from the build context (not --from=).
    # Sources are relative to gateway/; "." (the whole context) becomes the gateway/ prefix itself.
    inputs=$({ echo "$dockerfile"; awk '$1 == "COPY" && !/--from=/ { for (i = 2; i < NF; i++) if ($i !~ /^--/) {
                   src = $i; sub(/^\.\//, "", src); if (src == ".") src = ""; print "gateway/" src } }' "$dockerfile"; } \
        | sed 's/[.]/\\./g' | paste -sd '|' -)
    grep -qE "^($inputs)" <<<"$changed" && build=true
fi

echo "Build the image: $build${range:+ (changes in $range)}" >&2
echo "$build"
