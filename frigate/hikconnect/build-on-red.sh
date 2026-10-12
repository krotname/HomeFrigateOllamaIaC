#!/usr/bin/env bash
# Trusted fetchers may use the network; third-party compilation is offline.
set -euo pipefail
if [[ $(hostname) != adler-red-u3 || $(id -u) != 1000 ]]; then
  echo 'Run as the unprivileged krt user on adler-red-u3.' >&2
  exit 1
fi
revision=${1:?Pass the exact main commit SHA}
[[ $revision =~ ^[0-9a-f]{40}$ ]] || exit 2
git merge-base --is-ancestor "$revision" origin/main
git diff --exit-code "$revision" -- frigate/hikconnect >/dev/null
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
build_dir=$(mktemp -d /tmp/hikconnect-build.XXXXXXXX)
echo "Build directory: $build_dir"
go_revision=b5c61c18c26710d93c47e8efec9dc7ec14205c54
go_image=golang:1.26.1-alpine@sha256:2389ebfa5b7f43eeafbd6be0c3700cc46690ef842ad962f6c5bd6be49ed82039
python_image=python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f
mkdir -p "$build_dir"/{source,modules,cache,out/wheels}
curl --fail --location --max-time 120 \
  "https://codeload.github.com/pedropaulovc/go2rtc/tar.gz/$go_revision" -o "$build_dir/source.tar.gz"
tar --extract --gzip --file "$build_dir/source.tar.gz" --directory "$build_dir/source" --strip-components=1
for image in "$go_image" "$python_image"; do
  if command -v arc-cache >/dev/null; then
    arc-cache oci-pull "$image" || docker pull "$image"
  else
    docker pull "$image"
  fi
done
limits=(--rm --user 1000:1000 --read-only --cap-drop ALL --security-opt no-new-privileges
        --memory 3g --cpus 2 --pids-limit 256 --tmpfs '/tmp:rw,noexec,nosuid,size=256m,uid=1000,gid=1000')
timeout 180 docker run "${limits[@]}" \
  -v "$build_dir/source:/source:ro" -v "$build_dir/modules:/modules" -w /source \
  -e GOMODCACHE=/modules -e GOTOOLCHAIN=local "$go_image" go mod download
timeout 180 docker run "${limits[@]}" --network none \
  -v "$build_dir/source:/source:ro" -v "$build_dir/modules:/modules:ro" \
  -v "$build_dir/cache:/cache" -v "$build_dir/out:/out" -w /source \
  -e GOMODCACHE=/modules -e GOCACHE=/cache -e GOTOOLCHAIN=local -e CGO_ENABLED=0 \
  "$go_image" go build -buildvcs=false -trimpath -o /out/go2rtc .
timeout 120 docker run "${limits[@]}" \
  -v "$build_dir/out/wheels:/wheels" "$python_image" \
  python -m pip download --only-binary=:all: --no-cache-dir --dest /wheels \
  cryptography==46.0.3 cffi==2.0.0 pycparser==2.23
cp -- "$source_dir"/{Dockerfile,relay.py,go2rtc.yml} "$build_dir/out/"
timeout 120 docker build --network none --label "org.opencontainers.image.revision=$revision" \
  -t "krt/hikconnect-relay:$revision" "$build_dir/out"
docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
  "krt/hikconnect-relay:$revision"
echo "Export with: docker save -o $build_dir/relay-image.tar krt/hikconnect-relay:$revision"
echo 'Remove the temporary build directory after transfer and runtime verification.'
