# process-drone.ps1 - RealityScan 드론 사진 자동 처리 (폴더 감시형)
# 사용법: powershell -ExecutionPolicy Bypass -File .\process-drone.ps1
# 작업 투입: C:\DroneJobs\inbox\<작업이름>\ 폴더에 드론 사진(JPG)을 복사한다.
# 결과:     C:\DroneJobs\output\<작업이름>\ 에 tiles\tileset.json, <작업이름>.glb, .rsproj, 로그가 생긴다.
param(
  [string]$Inbox        = "C:\DroneJobs\inbox",
  [string]$Output       = "C:\DroneJobs\output",
  [string]$RS           = "C:\Program Files\Epic Games\RealityScan\RealityScan.exe",
  [int]   $Triangles    = 3000000,   # 텍스처를 입힐 단순화 모델의 삼각형 수
  [int]   $MinImages    = 20,        # 이보다 적으면 업로드 미완료로 간주
  [int]   $StableMinutes= 3,         # 마지막 파일 추가 후 이 시간 동안 변화 없으면 업로드 완료
  [int]   $PollSeconds  = 60
)

New-Item -ItemType Directory -Force -Path $Inbox, $Output | Out-Null
if (-not (Test-Path $RS)) { Write-Error "RealityScan.exe 없음: $RS"; exit 1 }

function Write-Log([string]$path, [string]$msg) {
  $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
  $line | Tee-Object -FilePath $path -Append
}

while ($true) {
  foreach ($job in (Get-ChildItem -Path $Inbox -Directory)) {
    $out = Join-Path $Output $job.Name
    if ((Test-Path (Join-Path $out "DONE.txt")) -or (Test-Path (Join-Path $out "FAILED.txt"))) { continue }

    $imgs = Get-ChildItem -Path $job.FullName -Recurse -File -Include *.jpg, *.jpeg, *.png
    if ($imgs.Count -lt $MinImages) { continue }
    $newest = ($imgs | Sort-Object LastWriteTime -Descending | Select-Object -First 1).LastWriteTime
    if (((Get-Date) - $newest) -lt [TimeSpan]::FromMinutes($StableMinutes)) { continue }   # 아직 업로드 중

    New-Item -ItemType Directory -Force -Path $out, (Join-Path $out "tiles") | Out-Null
    $log  = Join-Path $out "realityscan.log"
    $proj = Join-Path $out ("{0}.rsproj" -f $job.Name)
    Write-Log $log ("START {0} ({1} images)" -f $job.Name, $imgs.Count)

    $rsArgs = @(
      "-headless", "-stdConsole", "-printProgress",
      "-silent", "`"$out`"",
      "-newScene",
      "-addFolder", "`"$($job.FullName)`"",
      "-align",
      "-selectMaximalComponent",
      "-setReconstructionRegionAuto",
      "-calculateNormalModel",
      "-save", "`"$proj`"",
      "-simplify", $Triangles,
      "-calculateTexture",
      "-setOutputCoordinateSystem", "epsg:4326",
      "-export3dTiles", "`"$(Join-Path $out 'tiles\tileset.json')`"",
      "-setOutputCoordinateSystem", "Local:1",
      "-exportSelectedModel", "`"$(Join-Path $out ($job.Name + '.glb'))`"",
      "-save",
      "-quit"
    )
    Write-Log $log ("CMD  " + ($rsArgs -join " "))

    $p = Start-Process -FilePath $RS -ArgumentList $rsArgs -Wait -PassThru -NoNewWindow `
         -RedirectStandardOutput (Join-Path $out "stdout.log") -RedirectStandardError (Join-Path $out "stderr.log")

    $tileset = Join-Path $out "tiles\tileset.json"
    if ($p.ExitCode -eq 0 -and (Test-Path $tileset)) {
      Write-Log $log ("DONE exit=0, tileset={0}" -f $tileset)
      "ok" | Set-Content -Encoding utf8 (Join-Path $out "DONE.txt")
    } else {
      Write-Log $log ("FAILED exit={0}  (stderr.log, {1} 확인)" -f $p.ExitCode, (Join-Path $out "*.txt 크래시 리포트"))
      "exit=$($p.ExitCode)" | Set-Content -Encoding utf8 (Join-Path $out "FAILED.txt")
    }
  }
  Start-Sleep -Seconds $PollSeconds
}
