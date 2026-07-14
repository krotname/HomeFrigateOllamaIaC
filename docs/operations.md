# Operations

## Windows Host Access

Use WinRM over HTTPS for `ADLER-WHITE-1W`. Do not use raw SSH for normal
Windows host administration.

```powershell
$cred = Import-Clixml -LiteralPath 'C:\Users\KRT\.codex\secrets\adler-winrm.credential.xml'
Invoke-Command -ComputerName ADLER-WHITE-1W -UseSSL -ConfigurationName PowerShell.7 -Credential $cred -Authentication Negotiate -ScriptBlock {
    hostname
    whoami
    $PSVersionTable.PSVersion.ToString()
}
```

Expected host identity is `ADLER-WHITE-1W\codex-winrm`, PowerShell `7.6.2`
Core, `FullLanguage`, and an administrator token. SSH is retained only for
bootstrap/recovery of WinRM itself.

## Deploy

1. Copy `ansible/inventory.example.yml` to `ansible/inventory.yml`.
2. Copy `ansible/group_vars/all.example.yml` to `ansible/group_vars/all.yml`.
3. Put real camera credentials in `ansible/group_vars/all.yml`, or encrypt them:

```powershell
ansible-vault encrypt ansible/group_vars/all.yml
```

Before the first Ansible connection, obtain the VM SSH host key with
`ssh-keyscan`, compare its fingerprint with
`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the VM console, and only
then append the verified public key to the operator's `known_hosts`. The sample
inventory uses `StrictHostKeyChecking=yes` and will not learn a key silently.

4. Run:

```powershell
ansible-playbook -i ansible/inventory.yml ansible/playbooks/site.yml --ask-become-pass
```

## Hyper-V Host

Run against the Windows Server host through WinRM HTTPS:

```powershell
$cred = Import-Clixml -LiteralPath 'C:\Users\KRT\.codex\secrets\adler-winrm.credential.xml'
Invoke-Command -ComputerName ADLER-WHITE-1W -UseSSL -ConfigurationName PowerShell.7 -Credential $cred -Authentication Negotiate -FilePath .\scripts\hyperv-host-setup.ps1
```

Use `-AssignGpu` only when intentionally assigning the Tesla P40 DDA device to
the VM. That operation changes host PCI device state.

## Trust Local Certificate

Copy the public certificate over an authenticated channel and run on a Windows
client:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-frigate-local-ca.ps1 `
  -FrigateUrl https://192.168.1.138:8971 `
  -CaCertPath C:\secure-transfer\fullchain.pem
```

If the Frigate service uses the local root CA from the server, copy the public
CA and CRL to the Windows client first, then install both explicitly:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-frigate-local-ca.ps1 -CaCertPath $env:TEMP\KRT-Frigate-Local-Root-CA-2026.pem -CrlPath $env:TEMP\KRT-Frigate-Local-Root-CA-2026.crl
```

Alternatively, pass an out-of-band SHA-256 with
`-ExpectedSha256Thumbprint`. Bare remote capture is rejected;
`-TrustOnFirstUse` is an explicit bootstrap-only escape hatch.

## Config Backups

Backups are configuration-only and are retained only on the home server file
storage. The policy is documented in [backup-policy.md](backup-policy.md), and
the persistent registry is `registries/backup-registry.csv`.

The production scheduled task on `ADLER-WHITE-1W` is:

```text
WinHome Config Backup
```

It runs daily at `03:20` as `SYSTEM` and stores verified archives under:

```text
F:\Files\Backups\win-home-configs
```

Run the same backup manually through WinRM:

```powershell
$cred = Import-Clixml -LiteralPath 'C:\Users\KRT\.codex\secrets\adler-winrm.credential.xml'
Invoke-Command -ComputerName ADLER-WHITE-1W -UseSSL -ConfigurationName PowerShell.7 -Credential $cred -Authentication Negotiate -FilePath .\scripts\invoke-config-backup.ps1
```

## Smoke Test

Run after deploy and after host reboots:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke-test.ps1
```

All LAN APIs are protected by nginx basic auth. Provide the shared credentials
with the `-FrigateAuth*`, `-OllamaAuth*`, and `-AsrAuth*` parameters or the
matching `FRIGATE_BASIC_*`, `OLLAMA_BASIC_*`, and `ASR_BASIC_*` environment
variables.

The test writes `scripts\logs\frigate-vm-smoke-latest.json` unless a custom
`-ReportPath` is supplied. It verifies Hyper-V autostart, DDA GPU assignment,
trusted TLS, Frigate health, ONNX GPU detector, CUDA ffmpeg, camera FPS,
recordings, Ollama service/API, Frigate-to-Ollama network path and Ollama
text GPU execution. It also checks the separate ASR HTTPS API and container.

To include a real ASR transcription check, pass a local audio sample:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke-test.ps1 -AsrSamplePath "C:\path\audio.m4a" -SkipOllamaGenerate
```

## LAN API Usage

Install and verify the local certificate with `install-frigate-local-ca.ps1`
before using these endpoints. The examples intentionally do not disable TLS
verification.

Frigate is published on the VM LAN address:

```powershell
curl.exe -u "$env:FRIGATE_BASIC_USER`:$env:FRIGATE_BASIC_PASSWORD" https://192.168.1.138:8971/api/version
```

Ollama is published on the VM LAN address:

```powershell
curl.exe -u "$env:OLLAMA_BASIC_USER`:$env:OLLAMA_BASIC_PASSWORD" https://192.168.1.138:11443/api/version
```

ASR is published on the VM LAN address over HTTPS:

```powershell
curl.exe -u "$env:ASR_BASIC_USER`:$env:ASR_BASIC_PASSWORD" https://192.168.1.138:9443/health
```

Transcribe audio with the OpenAI-compatible endpoint:

```powershell
curl.exe -u "$env:ASR_BASIC_USER`:$env:ASR_BASIC_PASSWORD" -X POST "https://192.168.1.138:9443/v1/audio/transcriptions" `
  -F "file=@C:\path\audio.m4a" `
  -F "language=ru" `
  -F "response_format=json"
```

Run the installed gpt-oss model from this workstation:

```powershell
$body = @{
  model = "huihui_ai/gpt-oss-abliterated:20b"
  prompt = "Напиши одно короткое предложение по-русски."
  stream = $false
  think = "low"
  options = @{ num_predict = 256; num_ctx = 2048 }
} | ConvertTo-Json -Depth 4
curl.exe -u "$env:OLLAMA_BASIC_USER`:$env:OLLAMA_BASIC_PASSWORD" `
  --max-time 600 -H "Content-Type: application/json" `
  --data-binary $body https://192.168.1.138:11443/api/generate
```

For this gpt-oss model, keep `num_predict` at `256` or higher for normal visible
answers; lower limits may be spent on thinking tokens. Cold start on the Tesla
P40 takes about 5 minutes, so use a `600` second timeout for the first request.

## Rollback

On the VM, Frigate config backups are kept manually when changing production
config. To roll back a failed Frigate config:

```bash
sudo cp /opt/frigate/config/config.yml.bak-before-change /opt/frigate/config/config.yml
sudo docker compose -f /opt/frigate/docker-compose.yml up -d
```
