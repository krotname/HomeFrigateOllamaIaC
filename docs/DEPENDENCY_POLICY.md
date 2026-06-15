# Dependency Policy

## Current baseline

| Component | Version | Purpose |
|---|---:|---|
| Ansible Core | 2.21.0 | Playbook syntax and deployment runtime baseline |
| ansible-lint | 26.4.0 | Static analysis for playbooks and roles |
| yamllint | 1.38.0 | YAML formatting and parser checks |
| PSScriptAnalyzer | 1.25.0 | PowerShell static analysis in CI |
| Frigate image | `ghcr.io/blakeblackshear/frigate:stable-tensorrt` | CUDA/TensorRT Frigate runtime |
| Ollama model | `qwen2.5vl:3b` | Local vision model used by Frigate GenAI review |

## Update rules

- Prefer stable releases from official project release channels.
- Keep validation tools pinned in `requirements-dev.txt`.
- Keep GitHub Actions pinned to full commit SHAs with the reviewed tag in a trailing comment.
- Review Frigate image changes manually because the image tag lives in Ansible variables, not in a Dockerfile.
- Treat model changes as operational changes: verify VRAM use, response latency, and smoke-test output before making them the default.
- Run the repository validation checks after dependency changes.

## Manual update check

```bash
python -m pip install -r requirements-dev.txt
yamllint .
ansible-lint ansible/playbooks/site.yml
ansible-playbook -i ansible/inventory.example.yml ansible/playbooks/site.yml --syntax-check -e @ansible/group_vars/all.example.yml
```
