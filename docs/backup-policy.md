# Backup Policy

This project keeps retained backups in one verified location on the home server:

`F:\Files\Backups\win-home-configs`

The automated job is configuration-only. It does not back up media archives,
Frigate recordings, VHDX disks, Docker images, model files, database files, or
generated logs.

## Schedule

- Task name: `WinHome Config Backup`
- Host: `ADLER-WHITE-1W`
- Runner: local `SYSTEM`
- Schedule: daily at `03:20`
- Script: `C:\ProgramData\KRT\ConfigBackup\Invoke-WinHomeConfigBackup.ps1`
- VM SSH key: `C:\ProgramData\KRT\ConfigBackup\ssh\win-home-codex_ed25519`
- Retention: `60` days, deleted only under `F:\Files\Backups\win-home-configs\config-backup-*`

## Scope

Each run creates one directory:

`F:\Files\Backups\win-home-configs\config-backup-YYYYMMDD-HHMMSS`

The directory contains:

- `windows-configs.zip`: Windows host configuration exports and selected config
  files.
- `vm-configs.tar.gz`: Ubuntu VM configuration files from `frigate-ubuntu`.
- `vm-inventory.json`: VM runtime inventory needed to interpret the VM config
  archive.
- `manifest.json`: hashes, byte counts, timestamp, scope, and verification
  flags.

Windows host backup contents include network settings, NetBIOS/binding state,
password policy metadata, local users/groups metadata, SMB share definitions,
non-Microsoft scheduled tasks, firewall export, WinRM config, Hyper-V metadata,
OpenSSH config, and selected application config directories.

VM backup contents include `/opt/frigate/config/config.yml`,
`/opt/frigate/docker-compose.yml`, `/opt/frigate/.env`, public Frigate TLS
certificate files, nginx config, Docker daemon config, netplan, SSH daemon
config, `fstab`, `hosts`, and Ollama systemd overrides when present.

## Exclusions

The backup script explicitly skips heavy runtime data and private key material:

- Frigate media and recordings.
- VM VHDX disk images.
- SQLite/DB files.
- ONNX/PT model files.
- log/temp/bak files.
- private keys such as `privkey.pem`, `ca-key.pem`, `*.key`, `*.pfx`, and SSH
  private keys.

The backup may still contain operational secrets from config files such as
`.env`; the retained directory ACL is restricted to `SYSTEM`, local
Administrators, and `ADLER-WHITE-1W\KRT`.

## Verification

Every run writes `manifest.json` and updates:

`F:\Files\Backups\win-home-configs\latest.json`

The manifest records SHA-256 hashes and byte counts for both archives. A run is
considered valid only when both `windows-configs.zip` and `vm-configs.tar.gz`
exist and are larger than 1 KB.

Manual verification:

```powershell
Get-Content 'F:\Files\Backups\win-home-configs\latest.json' | ConvertFrom-Json
Test-Path 'F:\Files\Backups\win-home-configs\latest.json'
```

Run on demand:

```powershell
$cred = Import-Clixml -LiteralPath 'C:\Users\KRT\.codex\secrets\adler-winrm.credential.xml'
Invoke-Command -ComputerName ADLER-WHITE-1W -UseSSL -ConfigurationName PowerShell.7 -Credential $cred -Authentication Negotiate -FilePath .\scripts\invoke-config-backup.ps1
```

The production copy runs from `C:\ProgramData\KRT\ConfigBackup` on the server.

## Network VPN Configuration Backups

Router and VPN hub configuration snapshots are retained separately because they
contain router credentials, AmneziaWG keys, PSKs, and server VPN secrets.

Storage root:

`F:\Files\Backups\network-vpn-configs`

Each manual run creates one directory:

`F:\Files\Backups\network-vpn-configs\network-vpn-config-YYYYMMDD-HHMMSS`

The directory contains:

- `openwrt-sysupgrade-backup.tar.gz`: OpenWrt router config backup.
- `openwrt-*.uci` and `openwrt-packages.txt`: router config/package evidence.
- `msk-vpn-configs.tar.gz`: Moscow VPS VPN config archive covering AmneziaWG,
  Hysteria, sing-box, OpenVPN/WireGuard remnants, UFW, and related sysctl/systemd
  files.
- `client-krt-home-amneziawg.conf`: current split-route client config.
- `manifest.json`: hashes, byte counts, timestamp, scope, and verification
  flags.

These archives are not committed to git. The repository tracks only this policy
and `registries/backup-registry.csv`.
