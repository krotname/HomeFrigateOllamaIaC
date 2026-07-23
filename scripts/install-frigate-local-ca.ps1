param(
    [string]$FrigateUrl = "https://192.168.1.138:8971",
    [string]$OutFile = "$env:TEMP\frigate-local-ca.cer",
    [string]$CaCertPath = "",
    [string]$ExpectedSha256Thumbprint = "",
    [string]$CrlPath = "",
    [switch]$MachineStore,
    [switch]$TrustOnFirstUse
)

$ErrorActionPreference = "Stop"

$uri = $null
if (-not [Uri]::TryCreate($FrigateUrl, [UriKind]::Absolute, [ref]$uri) -or
    $uri.Scheme -ne "https" -or
    [string]::IsNullOrWhiteSpace($uri.Host) -or
    -not [string]::IsNullOrEmpty($uri.UserInfo) -or
    -not [string]::IsNullOrEmpty($uri.Query) -or
    -not [string]::IsNullOrEmpty($uri.Fragment) -or
    $uri.AbsolutePath -ne "/") {
    throw "FrigateUrl must be an absolute HTTPS authority without credentials, path, query, or fragment."
}
$frigateEndpoint = $uri.GetLeftPart([UriPartial]::Authority)

if ($ExpectedSha256Thumbprint -notmatch '^[A-Fa-f0-9:\s-]*$') {
    throw "ExpectedSha256Thumbprint contains unsupported characters."
}
$normalizedExpectedThumbprint = $ExpectedSha256Thumbprint -replace "[^A-Fa-f0-9]", ""
if ($normalizedExpectedThumbprint -and $normalizedExpectedThumbprint.Length -ne 64) {
    throw "ExpectedSha256Thumbprint must contain exactly 64 hexadecimal characters."
}
if ([string]::IsNullOrWhiteSpace($CaCertPath) -and
    -not $normalizedExpectedThumbprint -and
    -not $TrustOnFirstUse) {
    throw "Provide CaCertPath or an out-of-band ExpectedSha256Thumbprint. Use TrustOnFirstUse only for deliberate bootstrap."
}
if (-not [string]::IsNullOrWhiteSpace($CaCertPath) -and -not (Test-Path -LiteralPath $CaCertPath -PathType Leaf)) {
    throw "CA certificate not found: $CaCertPath"
}
if (-not [string]::IsNullOrWhiteSpace($CrlPath) -and -not (Test-Path -LiteralPath $CrlPath -PathType Leaf)) {
    throw "CRL not found: $CrlPath"
}

if ([string]::IsNullOrWhiteSpace($CaCertPath)) {
    # The capture must use a synchronous TLS handshake: an async client such as
    # HttpClient runs the validation scriptblock on a thread-pool thread that has
    # no PowerShell runspace, so the callback throws and no certificate is captured.
    $capturedCertificate = $null
    $tcpClient = $null
    $sslStream = $null
    try {
        $tcpClient = [System.Net.Sockets.TcpClient]::new()
        if (-not $tcpClient.ConnectAsync($uri.Host, $uri.Port).Wait(15000)) {
            throw "Timed out connecting to $frigateEndpoint"
        }
        $sslStream = [System.Net.Security.SslStream]::new(
            $tcpClient.GetStream(),
            $false,
            {
                param($tlsSender, $certificate, $chain, $sslPolicyErrors)
                if ($certificate) {
                    $script:capturedCertificate = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new($certificate)
                }
                return $true
            }
        )
        $sslStream.ReadTimeout = 15000
        $sslStream.WriteTimeout = 15000
        $sslStream.AuthenticateAsClient($uri.Host)
        if (-not $capturedCertificate -and $sslStream.RemoteCertificate) {
            $capturedCertificate = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new($sslStream.RemoteCertificate)
        }
    }
    finally {
        if ($sslStream) { $sslStream.Dispose() }
        if ($tcpClient) { $tcpClient.Dispose() }
    }

    if (-not $capturedCertificate) {
        throw "Could not capture certificate from $FrigateUrl"
    }
    $certificate = $capturedCertificate
}
else {
    $certificate = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new($CaCertPath)
}

$sha256Bytes = $certificate.GetCertHash([System.Security.Cryptography.HashAlgorithmName]::SHA256)
$actualSha256Thumbprint = ([BitConverter]::ToString($sha256Bytes)).Replace("-", "")
if ($normalizedExpectedThumbprint -and
    $actualSha256Thumbprint -ne $normalizedExpectedThumbprint.ToUpperInvariant()) {
    throw "Certificate SHA-256 thumbprint mismatch. Expected $($normalizedExpectedThumbprint.ToUpperInvariant()), got $actualSha256Thumbprint."
}
$now = [DateTime]::UtcNow
if ($certificate.NotBefore.ToUniversalTime() -gt $now -or $certificate.NotAfter.ToUniversalTime() -le $now) {
    throw "Certificate is not currently valid: $($certificate.NotBefore) .. $($certificate.NotAfter)."
}
if ($TrustOnFirstUse -and -not $normalizedExpectedThumbprint -and [string]::IsNullOrWhiteSpace($CaCertPath)) {
    Write-Warning "Trust-on-first-use accepted SHA-256 $actualSha256Thumbprint. Verify it out of band."
}

$publicCertificateBytes = $certificate.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Cert)
$certificate.Dispose()
$certificate = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new($publicCertificateBytes)
$basicConstraintsExtension = $certificate.Extensions | Where-Object {
    $_.Oid.Value -eq "2.5.29.19"
} | Select-Object -First 1
$isCertificateAuthority = $false
if ($basicConstraintsExtension) {
    $basicConstraints = [System.Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]::new(
        $basicConstraintsExtension,
        $basicConstraintsExtension.Critical
    )
    $isCertificateAuthority = $basicConstraints.CertificateAuthority
}
if (-not [string]::IsNullOrWhiteSpace($CrlPath) -and -not $isCertificateAuthority) {
    throw "A CRL can be installed only with a CA certificate."
}
[System.IO.File]::WriteAllBytes($OutFile, $publicCertificateBytes)

$storeLocation = if ($MachineStore) { "LocalMachine" } else { "CurrentUser" }
$storeName = if ($isCertificateAuthority) { "Root" } else { "TrustedPeople" }
$store = New-Object System.Security.Cryptography.X509Certificates.X509Store($storeName, $storeLocation)
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

Write-Host "Installed $($certificate.Subject) into Cert:\$storeLocation\$storeName"
Write-Host "SHA-256: $actualSha256Thumbprint"
Write-Host "Exported certificate to $OutFile"

if (-not [string]::IsNullOrWhiteSpace($CrlPath)) {
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

$certificate.Dispose()
