# Security Policy

## Supported versions

Security fixes are handled on the default branch and the latest public release line.

## Reporting vulnerabilities

Do not open a public issue for suspected vulnerabilities, leaked secrets, camera access details, host access details, or exploit steps.

Report vulnerabilities through GitHub private vulnerability reporting:
https://github.com/krotname/HomeFrigateOllamaIaC/security/advisories/new

Include:

- affected commit or release tag,
- reproducible deployment or smoke-test step,
- sanitized logs with IPs, tokens, RTSP credentials, SSH keys, and hostnames redacted,
- impact assessment for the Windows host, Ubuntu VM, Frigate, Ollama, or camera network,
- suggested mitigation if available.

The maintainer aims to acknowledge valid reports within 48 hours and provide a remediation timeline after the impact is confirmed.

## Secrets

Do not commit real RTSP credentials, SSH keys, private certificate keys, camera URLs, production-like inventories, generated certificates, model artifacts, smoke-test reports, or logs.

Use `ansible/group_vars/all.yml` locally, encrypt it with Ansible Vault when it leaves the workstation, and rotate any exposed credential immediately.
