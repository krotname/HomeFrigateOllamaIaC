# Frigate/Ollama Hyper-V GPU Stack

Infrastructure-as-code repository for the home Frigate + Ollama video AI stack.

Current production shape:

- Windows Server host `ADLER-WHITE-1W` runs Hyper-V.
- Ubuntu VM `frigate-ubuntu` runs Frigate, Ollama and nginx.
- NVIDIA Tesla P40 is assigned to the VM through Hyper-V DDA.
- Frigate uses CUDA ffmpeg and ONNX GPU object detection.
- Frigate GenAI uses Ollama `qwen2.5vl:3b`.
- Frigate HTTPS is exposed on `8971`; Ollama HTTPS proxy is exposed on `11443`.

## Technology

The repo uses a pragmatic split:

- PowerShell for Windows Server / Hyper-V / DDA GPU setup.
- Ansible for Ubuntu VM package, service, template, certificate and Docker Compose management.
- Docker Compose for Frigate runtime.

This is a better fit than Terraform for this stack because most of the state is
host-level Hyper-V and guest-level Linux service configuration, not cloud
resources. It is also more complete than plain Docker Compose because Ollama,
nginx, certificates, YOLO model build and GPU checks live outside the Frigate
container.

## Layout

```text
ansible/
  inventory.example.yml
  group_vars/all.example.yml
  playbooks/site.yml
  roles/frigate_vm/
scripts/
  hyperv-host-setup.ps1
  install-frigate-local-ca.ps1
  smoke-test.ps1
docs/
  architecture.md
  operations.md
  current-state.md
frigate/
  .env.example
```

## Deploy

Prerequisites on the control machine:

- PowerShell 7 or Windows PowerShell 5.1.
- GitHub/SSH access to the Windows host and Ubuntu VM.
- Ansible installed in WSL/Linux or another Unix-like control environment.
- `ansible-playbook` can SSH into the Ubuntu VM as a sudo-capable user.

1. Prepare inventory and variables:

```powershell
Copy-Item .\ansible\inventory.example.yml .\ansible\inventory.yml
Copy-Item .\ansible\group_vars\all.example.yml .\ansible\group_vars\all.yml
```

2. Edit `ansible\group_vars\all.yml` and set real camera credentials. Do not
commit this file. For encrypted secrets:

```powershell
ansible-vault encrypt .\ansible\group_vars\all.yml
```

3. Configure the Windows Hyper-V host from an elevated PowerShell session on the
host:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\hyperv-host-setup.ps1
```

Use `-AssignGpu` only when the Tesla P40 is not already assigned to the VM.

4. Deploy the VM services:

```powershell
ansible-playbook -i .\ansible\inventory.yml .\ansible\playbooks\site.yml --ask-become-pass
```

5. Trust the local Frigate/Ollama certificate on a Windows client:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-frigate-local-ca.ps1 -FrigateUrl https://192.168.1.138:8971
```

## Verify

Run the full smoke test:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke-test.ps1
```

The expected result is `failed_count=0`. The test covers:

- Windows host identity and VM autostart.
- Hyper-V DDA Tesla P40 assignment.
- trusted TLS for Frigate and Ollama HTTPS proxy.
- Frigate container health and API.
- Frigate ONNX GPU detector with YOLOv9-t 320.
- CUDA ffmpeg decode/scale path.
- camera FPS and recent recordings.
- Ollama service/API/model.
- Frigate container access to Ollama.
- live Frigate frame sent to Ollama vision model with GPU execution.

Latest verified production report:

```text
2026-06-15 09:25, failed_count=0
ONNX detector: GPU, inference ~8.1 ms
Ollama vision: qwen2.5vl:3b, 100% GPU
```

## Secret Policy

The repository intentionally excludes:

- `ansible/group_vars/all.yml`
- `.env`
- TLS private keys and generated certificates
- generated ONNX/PT model artifacts
- smoke-test reports and local logs

Use `ansible/group_vars/all.example.yml` and `frigate/.env.example` as templates.
