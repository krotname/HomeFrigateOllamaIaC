#!/bin/sh
# Download the pinned OCR model into ./models and verify it byte for byte.
# Usage: sh fetch-models.sh [target-dir]   (default: ./models next to this script)
set -eu

repository=ggml-org/Qwen3-VL-2B-Instruct-GGUF
revision=ea6a11058182570be6436b9a2e4ee7f7b49f908d
target=${1:-$(dirname "$0")/models}

mkdir -p "$target"
while read -r name sha256; do
    [ -n "$name" ] || continue
    file="$target/$name"
    if [ -f "$file" ] && echo "$sha256  $file" | sha256sum --check --status; then
        echo "ok $name"
        continue
    fi
    curl --fail --location --show-error --silent --retry 3 \
        --connect-timeout 30 --max-time 3600 \
        --output "$file.partial" \
        "https://huggingface.co/$repository/resolve/$revision/$name"
    echo "$sha256  $file.partial" | sha256sum --check --status || {
        echo "SHA256 mismatch for $name; partial file kept for diagnosis" >&2
        exit 1
    }
    chmod 0644 "$file.partial"
    mv "$file.partial" "$file"
    echo "fetched $name"
done <<'EOF'
Qwen3-VL-2B-Instruct-Q8_0.gguf b7802e29f71a9e5b5e3f83f613df898a2204342dcea71a231ea501d481813c39
mmproj-Qwen3-VL-2B-Instruct-Q8_0.gguf 69066c8f279ec85ff48ab4059f6ebba0d2932ca57667f2bbdac7d9805bca9e7b
EOF
