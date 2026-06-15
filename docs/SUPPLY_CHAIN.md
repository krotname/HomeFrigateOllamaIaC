# Supply Chain Verification

This repository uses a lightweight supply-chain baseline appropriate for home infrastructure as code.

## Workflow dependency integrity

GitHub Actions are pinned to full commit SHAs with the reviewed version kept in a trailing comment. Docker-based workflow steps are pinned by digest.

## Dev tool dependencies

Python-based validation tools are pinned in `requirements-dev.txt` and tracked by Dependabot:

- `ansible-core`
- `ansible-lint`
- `yamllint`

PowerShell validation uses a pinned PSScriptAnalyzer version in CI.

## Runtime image dependency

The Frigate image is configured in `ansible/group_vars/all.example.yml` as `frigate_image`. Dependabot does not automatically update this Ansible variable, so image changes must be reviewed manually with the Frigate release notes and validated on the target VM.

## Release verification

For tagged releases, download the release assets and verify checksums:

```bash
sha256sum -c checksums.txt
```

Verify provenance for the source archive:

```bash
gh attestation verify HomeFrigateOllamaIaC-vX.Y.Z.tar.gz \
  -R krotname/HomeFrigateOllamaIaC
```
