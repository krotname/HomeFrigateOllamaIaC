# Governance

## Protected branch

`main` is the default production branch. Changes are expected to pass the required GitHub Actions checks before they are considered releasable:

- IaC validation: YAML lint, Ansible lint, and playbook syntax check against example variables.
- PowerShell validation: parser check and PSScriptAnalyzer errors.
- Secret hygiene check for obvious committed credentials.
- CodeQL analysis for GitHub Actions workflows.
- GitHub Actions workflow linting through actionlint.
- OSSF Scorecards.
- Dependency Review on pull requests.

Direct maintainer updates are reserved for repository hygiene work that must land quickly. Feature, deployment behavior, host, VM, GPU, camera, and certificate changes should use pull requests.

## Review standards

- Keep infrastructure changes small enough to review by operational risk.
- Do not mix host setup, VM service changes, camera configuration, and documentation rewrites unless they must ship together.
- Treat Ansible roles, PowerShell scripts, GitHub Actions, and documentation as production code.
- Include rollback notes for changes that can affect remote access, GPU passthrough, TLS trust, Frigate storage, Ollama service state, or camera streams.
- Keep public examples sanitized and keep real values in ignored local files or Ansible Vault.

## Release accountability

Tagged releases are source snapshots, not deployable binaries. The release workflow creates a source archive, publishes SHA-256 checksums, and emits GitHub artifact attestations for provenance.

## Branch governance exception

This is a solo-maintained home infrastructure repository. Maintainer direct pushes may be used for urgent repository hygiene and security hardening, while behavior changes should still go through reviewed pull requests when practical.
