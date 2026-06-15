# Operations

## Deploy

1. Copy `ansible/inventory.example.yml` to `ansible/inventory.yml`.
2. Copy `ansible/group_vars/all.example.yml` to `ansible/group_vars/all.yml`.
3. Put real camera credentials in `ansible/group_vars/all.yml`, or encrypt them:

```powershell
ansible-vault encrypt ansible/group_vars/all.yml
```

4. Run:

```powershell
ansible-playbook -i ansible/inventory.yml ansible/playbooks/site.yml --ask-become-pass
```

## Hyper-V Host

Run on the Windows Server host:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\hyperv-host-setup.ps1
```

Use `-AssignGpu` only when intentionally assigning the Tesla P40 DDA device to
the VM. That operation changes host PCI device state.

## Trust Local Certificate

Run on a Windows client:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-frigate-local-ca.ps1 -FrigateUrl https://192.168.1.138:8971
```

## Smoke Test

Run after deploy and after host reboots:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke-test.ps1
```

The test writes `scripts\logs\frigate-vm-smoke-latest.json` unless a custom
`-ReportPath` is supplied. It verifies Hyper-V autostart, DDA GPU assignment,
trusted TLS, Frigate health, ONNX GPU detector, CUDA ffmpeg, camera FPS,
recordings, Ollama service/API, Frigate-to-Ollama network path and Ollama
vision GPU execution.

## Rollback

On the VM, Frigate config backups are kept manually when changing production
config. To roll back a failed Frigate config:

```bash
sudo cp /opt/frigate/config/config.yml.bak-before-change /opt/frigate/config/config.yml
sudo docker compose -f /opt/frigate/docker-compose.yml up -d
```
