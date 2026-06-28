# Reviewer Guide

## What this repository demonstrates

This repository describes a reproducible home video/audio AI stack where a Windows Server host runs a Hyper-V Ubuntu VM, passes an NVIDIA Tesla GPU through with DDA, and deploys Frigate, Ollama, and ASR services through Ansible.

The important boundaries are:

- PowerShell owns Windows host and Hyper-V setup.
- Ansible owns Ubuntu VM packages, services, templates, TLS material, and Docker Compose runtime files.
- Docker Compose owns the Frigate and ASR container runtimes.
- Local ignored files own camera credentials, inventory, private certificates, generated models, and smoke-test reports.

## Static checks

Run from the repository root:

```bash
python -m pip install -r requirements-dev.txt
python -m yamllint .github ansible .yamllint.yml
ansible-lint ansible/playbooks/site.yml
ansible-playbook -i ansible/inventory.example.yml ansible/playbooks/site.yml --syntax-check -e @ansible/group_vars/all.example.yml
```

PowerShell scripts can be parsed and tested without touching infrastructure:

```powershell
Get-ChildItem .\scripts -Filter *.ps1 -Recurse | ForEach-Object {
    $tokens = $null
    $errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($_.FullName, [ref]$tokens, [ref]$errors) | Out-Null
    if ($errors) { $errors; exit 1 }
}

Invoke-Pester -Path tests -CI
```

## Runtime smoke test

Only run the runtime smoke test when the real Windows host, Ubuntu VM, GPU, camera network, Frigate, Ollama, ASR, and local certificate trust are available:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke-test.ps1
```

Expected production signal from the last documented run is `failed_count=0`.

## Review focus

- Host and VM changes must preserve remote access and rollback paths.
- GPU passthrough changes must be reviewed against the current Tesla card and Windows Server behavior.
- Camera, RTSP, and certificate examples must remain sanitized.
- Frigate, Ollama, and ASR model updates must include smoke-test evidence or an explicit reason why runtime validation was deferred.
