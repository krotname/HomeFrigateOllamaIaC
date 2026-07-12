# Frigate + Ollama + ASR on a Hyper-V VM with NVIDIA Tesla

[Russian](README.md)


A reproducible IaC repository for a home video/audio AI stack: Frigate watches cameras, records footage, detects objects on GPU, Ollama serves a local text model, and a separate HTTPS ASR service transcribes audio with faster-whisper. Windows Server remains the main host, while the Linux/CUDA stack runs inside an Ubuntu VM.

Verified configuration:

- Windows Server host: `ADLER-WHITE-1W`.
- Hyper-V VM: `frigate-ubuntu`.
- GPU: NVIDIA Tesla P40 through Hyper-V DDA passthrough.
- Frigate: CUDA ffmpeg plus ONNX GPU detector YOLOv9-t 320.
- Ollama: `huihui_ai/gpt-oss-abliterated:20b`.
- ASR: `Systran/faster-whisper-large-v3`, CUDA `int8`.
- Frigate HTTPS LAN: `https://192.168.1.138:8971/`.
- Ollama HTTPS LAN: `https://192.168.1.138:11443/`.
- ASR HTTPS LAN: `https://192.168.1.138:9443/`.

## What This Repository Demonstrates

- Reproducible IaC for a Windows Server + Hyper-V + Ubuntu VM home stack.
- Clear split between PowerShell, Ansible, Docker Compose, and runtime smoke tests.
- GPU passthrough through Hyper-V DDA for constant Frigate/Ollama workload.
- Demonstration configs with Russian comments instead of empty placeholders.
- A validated OpenCode client configuration for the local Ollama models.
- CI checks for YAML, Ansible, PowerShell, GitHub Actions, CodeQL, and OpenSSF Scorecard.
- Documented governance, review, and supply-chain rules for a solo-maintained infrastructure repository.

## Why This Exists

Frigate on CPU alone quickly becomes CPU-bound: RTSP decoding, frame scaling, object detection, recording, and GenAI processing compete for the same cores. The result is higher latency, skipped frames, and increased host load.

Tesla P40 works well for this home server use case:

- `24 GB` VRAM is enough for the Frigate detector and a small vision LLM at the same time.
- The card is designed for 24/7 server workloads.
- Pascal `compute capability 6.1` is still supported by the CUDA/ONNXRuntime stack used here.
- Frigate offloads decode/scale and ONNX detection to GPU.
- Ollama keeps the local text model on GPU instead of swap/CPU.
- The VM isolates Linux NVIDIA runtime from Windows Server and Docker Desktop.

## Technology Split

- PowerShell: Windows Server, Hyper-V, VM autostart, DDA GPU passthrough.
- Ansible: Ubuntu VM packages, services, templates, certificates, Docker Compose.
- Docker Compose: Frigate and ASR runtime.

Terraform is not the primary tool here because the core resources are not cloud resources: they live in Hyper-V and inside a specific Ubuntu VM.

## Repository Layout

```text
ansible/
  inventory.example.yml
  group_vars/all.example.yml
  playbooks/site.yml
  roles/frigate_vm/
scripts/
  init-local-config.ps1
  hyperv-host-setup.ps1
  install-frigate-local-ca.ps1
  smoke-test.ps1
docs/
  architecture.md
  operations.md
  current-state.md
frigate/
  .env.example
asr/
  app.py
  docker-compose.yml
  Dockerfile
opencode/
  opencode.example.json
  AGENTS.md
  models/
```

See [docs/opencode-ollama.md](docs/opencode-ollama.md) for OpenCode client setup.

## Quick Start

Requirements:

- Windows PowerShell 5.1 or PowerShell 7.
- SSH access to the Windows host and Ubuntu VM.
- Ansible in WSL/Linux or another Unix-like control machine.
- A sudo user inside the Ubuntu VM.

Create local settings:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\init-local-config.ps1
```

Configure the Windows Server host:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\hyperv-host-setup.ps1
```

Assign GPU only when intentionally needed:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\hyperv-host-setup.ps1 -AssignGpu
```

Deploy services inside the Ubuntu VM:

```powershell
ansible-playbook -i .\ansible\inventory.yml .\ansible\playbooks\site.yml --ask-become-pass
```

Install the local certificate on the Windows client:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-frigate-local-ca.ps1 `
  -FrigateUrl https://192.168.1.138:8971 `
  -CaCertPath C:\secure-transfer\fullchain.pem
```

Run the full smoke test:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke-test.ps1
```

Expected result: `failed_count=0`.

The generated local variables include one basic-auth account for all three LAN
APIs. Set matching `FRIGATE_BASIC_*`, `OLLAMA_BASIC_*`, and `ASR_BASIC_*`
environment variables before running the smoke test.

Install and verify the local certificate with `install-frigate-local-ca.ps1`
first. The examples intentionally keep TLS certificate verification enabled.

ASR health check:

```powershell
curl.exe -u "$env:ASR_BASIC_USER`:$env:ASR_BASIC_PASSWORD" https://192.168.1.138:9443/health
```

Audio transcription:

```powershell
curl.exe -u "$env:ASR_BASIC_USER`:$env:ASR_BASIC_PASSWORD" -X POST "https://192.168.1.138:9443/v1/audio/transcriptions" `
  -F "file=@C:\path\audio.m4a" `
  -F "language=ru" `
  -F "response_format=json"
```

The full proof snapshot is in [docs/smoke-test-proof.md](docs/smoke-test-proof.md).

## Release

The first public release is
[v0.1.0](https://github.com/krotname/HomeFrigateOllamaIaC/releases/tag/v0.1.0).
It publishes the source archive, `checksums.txt`, and GitHub build provenance
attestation for the tagged archive.

## Security and Quality

- [Security Policy](SECURITY.md) covers private vulnerability reporting and secret rules.
- [Governance](docs/GOVERNANCE.md) records protected branch baseline and solo-maintained hygiene exceptions.
- [Reviewer Guide](docs/REVIEWER_GUIDE.md) lists static checks and the runtime smoke-test.
- [Smoke-Test Proof](docs/smoke-test-proof.md) records the latest production run with `failed_count=0`.
- [Supply Chain Verification](docs/SUPPLY_CHAIN.md) explains SHA-pinned Actions, pinned dev tools and release attestations.
- [Dependency Policy](docs/DEPENDENCY_POLICY.md) records the current Ansible/PowerShell/Frigate/Ollama baseline.
