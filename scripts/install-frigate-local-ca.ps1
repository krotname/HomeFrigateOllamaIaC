param(
    [string]$FrigateUrl = "https://192.168.1.138:8971",
    [string]$OutFile = "$env:TEMP\frigate-local-ca.cer",
    [string]$CaCertPath = "",
    [string]$CrlPath = "",
    [switch]$MachineStore
)

$ErrorActionPreference = "Stop"

$uri = [Uri]$FrigateUrl
if ($uri.Scheme -ne "https") {
    throw "FrigateUrl must use https so a certificate can be captured safely"
}

if ([string]::IsNullOrWhiteSpace($CaCertPath)) {
    $capturedCertificate = $null
    $handler = [System.Net.Http.HttpClientHandler]::new()
    $handler.ServerCertificateCustomValidationCallback = {
        param($requestMessage, $certificate, $chain, $sslPolicyErrors)
        if ($certificate) {
            $script:capturedCertificate = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new($certificate)
        }
        return $true
    }
    $client = [System.Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromSeconds(15)
    try {
        $response = $client.GetAsync("$FrigateUrl/api/version").GetAwaiter().GetResult()
        try {
            $response.EnsureSuccessStatusCode() | Out-Null
        }
        finally {
            $response.Dispose()
        }
    }
    catch {
        if (-not $capturedCertificate) {
            throw
        }
    }
    finally {
        $client.Dispose()
        $handler.Dispose()
    }

    if (-not $capturedCertificate) {
        throw "Could not capture certificate from $FrigateUrl"
    }
    $certificate = $capturedCertificate
}
else {
    if (-not (Test-Path -LiteralPath $CaCertPath)) {
        throw "CA certificate not found: $CaCertPath"
    }
    $certificate = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new($CaCertPath)
}

[System.IO.File]::WriteAllBytes($OutFile, $certificate.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Cert))

$storeLocation = if ($MachineStore) { "LocalMachine" } else { "CurrentUser" }
$store = New-Object System.Security.Cryptography.X509Certificates.X509Store("Root", $storeLocation)
$store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
try {
    $existing = $store.Certificates | Where-Object { $_.Thumbprint -eq $certificate.Thumbprint } | Select-Object -First 1
    if (-not $existing) {
        $store.Add($certificate)
    }
}
finally {
    $store.Close()
}

Write-Host "Installed $($certificate.Subject) into Cert:\$storeLocation\Root"
Write-Host "Exported certificate to $OutFile"

if (-not [string]::IsNullOrWhiteSpace($CrlPath)) {
    if (-not (Test-Path -LiteralPath $CrlPath)) {
        throw "CRL not found: $CrlPath"
    }
    $certutilArgs = @()
    if (-not $MachineStore) {
        $certutilArgs += "-user"
    }
    $certutilArgs += @("-addstore", "CA", $CrlPath)
    & certutil.exe @certutilArgs | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "certutil failed to install CRL from $CrlPath"
    }
}
