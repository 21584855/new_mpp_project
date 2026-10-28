param(
    [string]$Url = "http://127.0.0.1:8791/api/health",
    [int]$TimeoutSec = 5
)
try {
    $health = Invoke-RestMethod -Uri $Url -TimeoutSec $TimeoutSec
    if ($health.status -eq "ok") {
        Write-Host "ok nodes=$($health.nodes) edges=$($health.edges)"
        exit 0
    }
    Write-Host "unhealthy: $($health | ConvertTo-Json -Compress)"
    exit 1
} catch {
    Write-Host "unreachable: $($_.Exception.Message)"
    exit 1
}
