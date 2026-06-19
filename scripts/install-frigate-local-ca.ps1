param(
    [string]$FrigateUrl = "https://192.168.1.138:8971",
    [string]$OutFile = "$env:TEMP\frigate-local-ca.cer",
    [switch]$MachineStore
)

$ErrorActionPreference = "Stop"

$uri = [Uri]$FrigateUrl
if ($uri.Scheme -ne "https") {
    throw "FrigateUrl must use https so a certificate can be captured safely"
}

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
finally {
    $client.Dispose()
    $handler.Dispose()
}

if (-not $capturedCertificate) {
    throw "Could not capture certificate from $FrigateUrl"
}

[System.IO.File]::WriteAllBytes($OutFile, $capturedCertificate.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Cert))

$storeLocation = if ($MachineStore) { "LocalMachine" } else { "CurrentUser" }
$store = New-Object System.Security.Cryptography.X509Certificates.X509Store("Root", $storeLocation)
$store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
try {
    $store.Add($capturedCertificate)
}
finally {
    $store.Close()
}

Write-Host "Installed $($capturedCertificate.Subject) into Cert:\$storeLocation\Root"
Write-Host "Exported certificate to $OutFile"
