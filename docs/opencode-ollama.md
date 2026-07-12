# OpenCode client for Ollama

The files in `opencode/` configure OpenCode to use the authenticated
OpenAI-compatible Ollama endpoint without committing the URL, credentials, or
local CA certificate.

The configuration was runtime-tested with OpenCode `1.17.11` and Ollama
`0.30.8` on 2026-07-12. All four declared models returned a response. Qwen3
also completed a native `glob` tool call and then returned the requested JSON.

## Prepare the model tags

The OpenCode configuration expects four exact local tags. The general IaC
deployment pulls only its primary Ollama model, so create the client-specific
tags before installing this configuration. Run these commands on the Ollama VM
from a checkout or transferred copy that includes `opencode/models/`:

```bash
ollama_local() {
  sudo -u ollama -H env OLLAMA_HOST=http://127.0.0.1:11435 ollama "$@"
}

ollama_local pull huihui_ai/gpt-oss-abliterated:20b
ollama_local pull huihui_ai/qwen3-abliterated:8b
ollama_local pull huihui_ai/nemotron-v1-abliterated:8b-llama-3.1-nano
ollama_local pull huihui_ai/mistral-small-abliterated:24b

ollama_local create gpt-oss-uncensored:16k \
  -f opencode/models/gpt-oss-uncensored-16k.Modelfile
ollama_local create qwen3-uncensored:16k \
  -f opencode/models/qwen3-uncensored-16k.Modelfile
ollama_local create nemotron-uncensored:16k \
  -f opencode/models/nemotron-uncensored-16k.Modelfile
ollama_local create mistral-adler:16k \
  -f opencode/models/mistral-adler-16k.Modelfile
```

The host-local `11435` endpoint is created by this repository's nginx
deployment. Do not use the default `127.0.0.1:11434` on this VM.

The pulls are large. Do not repeat them during every client install. Existing
blobs are reused when the tags are already present.

## Install on Windows

Copy the example and global rules into the OpenCode configuration directory:

```powershell
$configDir = Join-Path $HOME ".config\opencode"
New-Item -ItemType Directory -Path $configDir -Force | Out-Null
Copy-Item .\opencode\opencode.example.json (Join-Path $configDir "opencode.json")
Copy-Item .\opencode\AGENTS.md (Join-Path $configDir "AGENTS.md")
```

Set the endpoint without a trailing slash because the configuration appends
`/v1`. Store the Base64 value of `username:password`, not the already prefixed
HTTP header:

```powershell
$basicUser = Read-Host "Ollama basic-auth user"
$basicPassword = Read-Host "Ollama basic-auth password" -AsSecureString
$credential = [pscredential]::new($basicUser, $basicPassword)
$pair = "{0}:{1}" -f $credential.UserName, $credential.GetNetworkCredential().Password
$basicB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($pair))

[Environment]::SetEnvironmentVariable(
    "ADLER_OLLAMA_URL",
    "https://ollama-host.example:11443",
    "User"
)
[Environment]::SetEnvironmentVariable("ADLER_BASIC_B64", $basicB64, "User")
[Environment]::SetEnvironmentVariable(
    "NODE_EXTRA_CA_CERTS",
    "C:\path\to\local-root-ca.pem",
    "User"
)
```

Open a new terminal after changing user environment variables. For immediate
validation in the current PowerShell process, reload them explicitly:

```powershell
$env:ADLER_OLLAMA_URL = [Environment]::GetEnvironmentVariable("ADLER_OLLAMA_URL", "User")
$env:ADLER_BASIC_B64 = [Environment]::GetEnvironmentVariable("ADLER_BASIC_B64", "User")
$env:NODE_EXTRA_CA_CERTS = [Environment]::GetEnvironmentVariable("NODE_EXTRA_CA_CERTS", "User")

Test-Path -LiteralPath $env:NODE_EXTRA_CA_CERTS
$resolved = opencode debug config | ConvertFrom-Json
[pscustomobject]@{
    ProviderPresent = $null -ne $resolved.provider.adler
    EndpointPresent = -not [string]::IsNullOrWhiteSpace($resolved.provider.adler.options.baseURL)
    AuthPresent = -not [string]::IsNullOrWhiteSpace(
        $resolved.provider.adler.options.headers.Authorization
    )
    Model = $resolved.model
    SmallModel = $resolved.small_model
}

$headers = @{ Authorization = "Basic $env:ADLER_BASIC_B64" }
$available = @(
    (Invoke-RestMethod -Uri "$env:ADLER_OLLAMA_URL/v1/models" -Headers $headers).data.id
)
$expected = @(
    "gpt-oss-uncensored:16k",
    "qwen3-uncensored:16k",
    "mistral-adler:16k",
    "nemotron-uncensored:16k"
)
$missing = @($expected | Where-Object { $_ -notin $available })
if ($missing.Count -gt 0) {
    throw "Missing Ollama model tags: $($missing -join ', ')"
}

opencode run --model adler/qwen3-uncensored:16k "Reply with exactly: OK"
```

Do not print or log the full `$resolved` object: it contains the resolved
`Authorization` header.

`OLLAMA_HOST` is not used by this OpenCode provider. Connection settings come
from `ADLER_OLLAMA_URL` and `ADLER_BASIC_B64`.

## Models

| OpenCode model | Purpose |
| --- | --- |
| `adler/gpt-oss-uncensored:16k` | Default reasoning model |
| `adler/qwen3-uncensored:16k` | Small model, titles, and native tool calls |
| `adler/mistral-adler:16k` | General 24B model |
| `adler/nemotron-uncensored:16k` | Compact non-reasoning model |

The first request after a cold model load can take several minutes. The client
timeout is therefore set to 15 minutes.

## Secret hygiene

Do not commit a real `ADLER_OLLAMA_URL`, `ADLER_BASIC_B64`, CA certificate,
password, or a resolved `Authorization` header. The committed configuration
contains environment placeholders only.
