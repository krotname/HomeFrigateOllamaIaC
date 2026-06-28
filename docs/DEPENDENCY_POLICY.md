# Dependency Policy

## Current baseline

| Component | Version | Purpose |
|---|---:|---|
| Ansible Core | 2.21.0 | Playbook syntax and deployment runtime baseline |
| ansible-lint | 26.4.0 | Static analysis for playbooks and roles |
| yamllint | 1.38.0 | YAML formatting and parser checks |
| PSScriptAnalyzer | 1.25.0 | PowerShell static analysis in CI |
| Pester | 5.7.1 | PowerShell unit tests |
| Frigate image | `ghcr.io/blakeblackshear/frigate:stable-tensorrt` | CUDA/TensorRT Frigate runtime |
| Ollama model | `huihui_ai/gpt-oss-abliterated:20b` | Local text model for LAN API usage |
| ASR base image | `nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04` | CUDA runtime for faster-whisper |
| ASR model | `Systran/faster-whisper-large-v3` | Local audio transcription |
| ASR Python runtime | `faster-whisper==1.1.1`, `fastapi==0.115.6` | HTTPS ASR API |

## Update rules

- Prefer stable releases from official project release channels.
- Keep validation tools pinned in `requirements-dev.txt`.
- Keep GitHub Actions pinned to full commit SHAs with the reviewed tag in a trailing comment.
- Review Frigate image changes manually because the image tag lives in Ansible variables, not in a Dockerfile.
- Treat model changes as operational changes: verify VRAM use, response latency, ASR sample quality, and smoke-test output before making them the default.
- Run the repository validation checks after dependency changes.

## Manual update check

```bash
python -m pip install -r requirements-dev.txt
python -m yamllint .github ansible .yamllint.yml
ansible-lint ansible/playbooks/site.yml
ansible-playbook -i ansible/inventory.example.yml ansible/playbooks/site.yml --syntax-check -e @ansible/group_vars/all.example.yml
pwsh -NoProfile -Command "Invoke-Pester -Path tests -CI"
```
