param(
    [string]$FrigateUrl = "https://192.168.1.138:8971",
    [string]$OutFile = "$env:TEMP\frigate-local-ca.cer",
    [switch]$MachineStore
)

$ErrorActionPreference = "Stop"

Add-Type @"
using System.Net.Security;
using System.Security.Cryptography.X509Certificates;
public class CertCapture {
  public static X509Certificate2 Certificate;
  public static bool Callback(object sender, X509Certificate certificate, X509Chain chain, SslPolicyErrors errors) {
    Certificate = new X509Certificate2(certificate);
    return true;
  }
}
"@

[System.Net.ServicePointManager]::ServerCertificateValidationCallback = [CertCapture]::Callback
try {
    Invoke-WebRequest -Uri "$FrigateUrl/api/version" -UseBasicParsing -TimeoutSec 15 | Out-Null
}
finally {
    [System.Net.ServicePointManager]::ServerCertificateValidationCallback = $null
}

if (-not [CertCapture]::Certificate) {
    throw "Could not capture certificate from $FrigateUrl"
}

[System.IO.File]::WriteAllBytes($OutFile, [CertCapture]::Certificate.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Cert))

$storeLocation = if ($MachineStore) { "LocalMachine" } else { "CurrentUser" }
$store = New-Object System.Security.Cryptography.X509Certificates.X509Store("Root", $storeLocation)
$store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
try {
    $store.Add([CertCapture]::Certificate)
}
finally {
    $store.Close()
}

Write-Host "Installed $([CertCapture]::Certificate.Subject) into Cert:\$storeLocation\Root"
Write-Host "Exported certificate to $OutFile"
