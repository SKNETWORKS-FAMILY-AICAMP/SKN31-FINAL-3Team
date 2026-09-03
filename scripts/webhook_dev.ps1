param(
    [int]$Port = 18000,
    [switch]$KeepWebhooks
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$RuntimeDir = Join-Path $ProjectRoot '.runtime'
$TunnelOut = Join-Path $RuntimeDir 'cloudflared.out.log'
$TunnelErr = Join-Path $RuntimeDir 'cloudflared.err.log'
$HealthUrl = "http://127.0.0.1:$Port/api/health"
$StartedBackend = $false
$BackendProcess = $null
$TunnelProcess = $null
$ConfigurationAttempted = $false

function Resolve-Cloudflared {
    $Command = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($Command) { return $Command.Source }
    $Candidates = @(
        'C:\Program Files\cloudflared\cloudflared.exe',
        'C:\Program Files (x86)\cloudflared\cloudflared.exe'
    )
    foreach ($Candidate in $Candidates) {
        if (Test-Path -LiteralPath $Candidate) { return $Candidate }
    }
    throw 'cloudflared가 없습니다. winget install Cloudflare.cloudflared 명령으로 설치해주세요.'
}

function Test-BackendHealth {
    try {
        $Health = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 2
        return $Health.service -eq 'biddingflow-webhook-gateway'
    } catch {
        return $false
    }
}

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python 가상환경을 찾을 수 없습니다: $Python"
}

New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
Set-Content -LiteralPath $TunnelOut -Value '' -Encoding utf8
Set-Content -LiteralPath $TunnelErr -Value '' -Encoding utf8

try {
    if (-not (Test-BackendHealth)) {
        Write-Host '[1/4] 웹훅 전용 FastAPI 게이트웨이를 시작합니다.' -ForegroundColor Cyan
        $BackendProcess = Start-Process `
            -FilePath $Python `
            -ArgumentList @('-m', 'uvicorn', 'webhook_gateway:app', '--host', '127.0.0.1', '--port', "$Port", '--timeout-graceful-shutdown', '3') `
            -WorkingDirectory $ProjectRoot `
            -WindowStyle Hidden `
            -PassThru
        $StartedBackend = $true
        for ($Attempt = 0; $Attempt -lt 30 -and -not (Test-BackendHealth); $Attempt++) {
            Start-Sleep -Milliseconds 500
        }
        if (-not (Test-BackendHealth)) {
            throw '웹훅 게이트웨이가 15초 안에 준비되지 않았습니다.'
        }
    } else {
        Write-Host '[1/4] 이미 실행 중인 웹훅 게이트웨이를 사용합니다.' -ForegroundColor Cyan
    }

    Write-Host '[2/4] Cloudflare 임시 HTTPS 터널을 시작합니다.' -ForegroundColor Cyan
    $Cloudflared = Resolve-Cloudflared
    $TunnelProcess = Start-Process `
        -FilePath $Cloudflared `
        -ArgumentList @('tunnel', '--url', "http://127.0.0.1:$Port", '--no-autoupdate') `
        -RedirectStandardOutput $TunnelOut `
        -RedirectStandardError $TunnelErr `
        -WindowStyle Hidden `
        -PassThru

    $PublicUrl = $null
    for ($Attempt = 0; $Attempt -lt 60 -and -not $PublicUrl; $Attempt++) {
        Start-Sleep -Milliseconds 500
        $CombinedLog = @(
            (Get-Content -LiteralPath $TunnelOut -Raw -ErrorAction SilentlyContinue),
            (Get-Content -LiteralPath $TunnelErr -Raw -ErrorAction SilentlyContinue)
        ) -join "`n"
        $Match = [regex]::Match($CombinedLog, 'https://[a-z0-9-]+\.trycloudflare\.com')
        if ($Match.Success) { $PublicUrl = $Match.Value }
        if ($TunnelProcess.HasExited) {
            throw "Cloudflare 터널이 조기 종료되었습니다. 로그: $TunnelErr"
        }
    }
    if (-not $PublicUrl) {
        throw "30초 안에 공개 터널 주소를 얻지 못했습니다. 로그: $TunnelErr"
    }
    Write-Host "      공개 주소: $PublicUrl" -ForegroundColor Green

    Write-Host ''
    Write-Warning '다음 단계는 ERPNext 웹훅을 임시 Cloudflare 주소로 변경합니다.'
    Write-Host '전송 범위: 문서 종류/이름/상태/수정시각과 연결 식별자입니다.'
    Write-Host 'File 이벤트에는 첨부 대상과 file_url도 포함됩니다. 품목·수량·금액 원문은 보내지 않습니다.'
    Write-Host '터널 종료 시 이 스크립트가 생성·갱신한 임시 웹훅은 기본적으로 비활성화됩니다.'
    $Approval = Read-Host '위 정보를 확인했고 로컬 웹훅 실험을 시작하려면 YES를 입력하세요'
    if ($Approval -cne 'YES') {
        throw '사용자가 ERPNext 임시 웹훅 등록을 취소했습니다.'
    }

    Write-Host '[3/4] ERPNext의 BiddingFlow 웹훅을 현재 터널 주소로 맞춥니다.' -ForegroundColor Cyan
    $ConfigurationAttempted = $true
    & $Python (Join-Path $PSScriptRoot 'configure_erpnext_webhooks.py') --base-url $PublicUrl --apply
    if ($LASTEXITCODE -ne 0) { throw 'ERPNext 웹훅 자동 구성에 실패했습니다.' }

    Write-Host '[4/4] ERPNext와 동일한 형식으로 공개 엔드포인트를 검증합니다.' -ForegroundColor Cyan
    $SecretLine = Get-Content -LiteralPath (Join-Path $ProjectRoot '.env') -Encoding utf8 |
        Where-Object { $_ -match '^ERPNEXT_WEBHOOK_SECRET=' } |
        Select-Object -First 1
    $Secret = ($SecretLine -split '=', 2)[1].Trim()
    if (-not $Secret) { throw 'ERPNEXT_WEBHOOK_SECRET이 비어 있습니다.' }
    $Headers = @{ 'X-ERPNext-Webhook-Secret' = $Secret }
    $Body = @{
        event = 'local_connectivity_test'
        doc = @{
            doctype = 'File'
            name = 'BIDDINGFLOW-CONNECTIVITY-TEST'
            attached_to_doctype = 'Connectivity Test'
        }
    } | ConvertTo-Json -Depth 5
    $Result = Invoke-RestMethod `
        -Method Post `
        -Uri "$PublicUrl/api/webhooks/erpnext/material-request-file" `
        -Headers $Headers `
        -ContentType 'application/json' `
        -Body $Body `
        -TimeoutSec 20
    if (-not $Result.accepted) { throw '공개 웹훅 검증 응답이 올바르지 않습니다.' }

    Write-Host ''
    Write-Host '로컬 웹훅 실험 준비가 완료되었습니다.' -ForegroundColor Green
    Write-Host '이 창을 열어둔 동안 ERPNext 이벤트가 로컬 FastAPI로 전달됩니다.'
    Write-Host '테스트를 마치려면 Enter를 누르세요.'
    $null = Read-Host
} finally {
    if (-not $KeepWebhooks -and $ConfigurationAttempted) {
        Write-Host 'ERPNext의 임시 로컬 웹훅을 비활성화합니다.' -ForegroundColor Yellow
        & $Python (Join-Path $PSScriptRoot 'configure_erpnext_webhooks.py') --disable --apply
    }
    if ($TunnelProcess -and -not $TunnelProcess.HasExited) {
        Stop-Process -Id $TunnelProcess.Id -Force
    }
    if ($StartedBackend -and $BackendProcess -and -not $BackendProcess.HasExited) {
        Stop-Process -Id $BackendProcess.Id
    }
}
