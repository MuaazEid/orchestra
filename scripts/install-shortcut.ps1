# Creates a "Launch Orchestra" shortcut on your desktop.
# Run this ONCE from PowerShell — after that, just double-click the
# shortcut. It opens a PowerShell window, pulls the latest code, and
# starts the hot-reload server. Ctrl+C to stop; close the window when done.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\install-shortcut.ps1

$ErrorActionPreference = "Stop"

$repoRoot   = "C:\dev\orchestra"
$devScript  = Join-Path $repoRoot "dev.ps1"
$iconPath   = Join-Path $repoRoot "scripts\orchestra.ico"
$desktop    = [Environment]::GetFolderPath("Desktop")
$linkPath   = Join-Path $desktop "Orchestra.lnk"

if (-not (Test-Path $devScript)) {
    Write-Error "Repo not found at $repoRoot. Run scripts\setup.ps1 first."
    exit 1
}

# Generate a simple, distinctive .ico from scratch (no external files needed):
# amber circle on the deep-navy Orchestra background, saved as a multi-size ICO.
Add-Type -AssemblyName System.Drawing
function New-OrchestraIcon($path) {
    $sizes = 16, 32, 48, 64, 128, 256
    $pngStreams = @()
    foreach ($size in $sizes) {
        $bmp = New-Object System.Drawing.Bitmap $size, $size
        $g   = [System.Drawing.Graphics]::FromImage($bmp)
        $g.SmoothingMode = "AntiAlias"
        $g.Clear([System.Drawing.Color]::FromArgb(255, 10, 14, 23))     # #0A0E17
        $pad = [Math]::Max(1, [int]($size * 0.18))
        $rect = New-Object System.Drawing.Rectangle $pad, $pad, ($size - 2*$pad), ($size - 2*$pad)
        $amber = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255, 232, 163, 61))  # #E8A33D
        $g.FillEllipse($amber, $rect)
        $g.Dispose()
        $ms = New-Object System.IO.MemoryStream
        $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
        $bmp.Dispose()
        $pngStreams += ,$ms
    }
    # Write ICO header + directory + PNG payloads
    $fs = [System.IO.File]::Create($path)
    $bw = New-Object System.IO.BinaryWriter $fs
    $bw.Write([UInt16]0); $bw.Write([UInt16]1); $bw.Write([UInt16]$sizes.Count)
    $offset = 6 + 16 * $sizes.Count
    for ($i = 0; $i -lt $sizes.Count; $i++) {
        $len = $pngStreams[$i].Length
        $s = $sizes[$i]
        $bw.Write([Byte]($s -band 0xFF))   # width (0 = 256)
        $bw.Write([Byte]($s -band 0xFF))   # height
        $bw.Write([Byte]0)                  # palette
        $bw.Write([Byte]0)                  # reserved
        $bw.Write([UInt16]1)                # planes
        $bw.Write([UInt16]32)               # bit depth
        $bw.Write([UInt32]$len)
        $bw.Write([UInt32]$offset)
        $offset += $len
    }
    foreach ($ms in $pngStreams) { $ms.WriteTo($fs); $ms.Dispose() }
    $bw.Close(); $fs.Close()
}

if (-not (Test-Path $iconPath)) {
    Write-Host "==> Generating icon" -ForegroundColor Cyan
    New-OrchestraIcon $iconPath
}

Write-Host "==> Creating desktop shortcut" -ForegroundColor Cyan
$wsh = New-Object -ComObject WScript.Shell
$sc = $wsh.CreateShortcut($linkPath)
$sc.TargetPath       = "powershell.exe"
$sc.Arguments        = "-NoExit -ExecutionPolicy Bypass -File `"$devScript`""
$sc.WorkingDirectory = $repoRoot
$sc.IconLocation     = "$iconPath,0"
$sc.Description      = "Launch Orchestra (pull + hot-reload server)"
$sc.Save()

Write-Host ""
Write-Host "Done. Double-click 'Orchestra' on your desktop to launch." -ForegroundColor Green
