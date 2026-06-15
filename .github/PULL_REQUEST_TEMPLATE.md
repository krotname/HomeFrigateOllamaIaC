## Summary

Briefly describe the infrastructure, documentation, or verification change and why it is needed.

## Testing

- [ ] `yamllint .`
- [ ] `ansible-lint ansible/playbooks/site.yml`
- [ ] `ansible-playbook -i ansible/inventory.example.yml ansible/playbooks/site.yml --syntax-check -e @ansible/group_vars/all.example.yml`
- [ ] PowerShell parser/PSScriptAnalyzer checks pass
- [ ] Runtime smoke test considered or run when host, VM, GPU, camera, or certificate behavior changed

## Quality checks

- [ ] No real camera URLs, RTSP credentials, SSH keys, certificates, tokens, logs, or generated models in the diff
- [ ] GitHub Actions remain SHA-pinned with the reviewed tag kept as a comment
- [ ] Documentation updated when setup, operations, security posture, or rollback behavior changed

## Operational impact

- Breaking changes:
- Rollback plan:
- Manual verification evidence:
