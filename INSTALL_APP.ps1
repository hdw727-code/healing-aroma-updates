[CmdletBinding()]
param(
    [string]$TestTarget,
    [switch]$SkipShortcut
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$installerSource = [IO.Path]::GetFullPath($PSScriptRoot)
$appSource = Join-Path $installerSource 'aroma_app'
$ownedStage = $null

function Assert-NoReparse([string]$Path) {
    $probe = [IO.Path]::GetFullPath($Path)
    while ($probe) {
        if (Test-Path -LiteralPath $probe) {
            $item = Get-Item -LiteralPath $probe -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw '링크 또는 연결 폴더에는 설치할 수 없습니다.'
            }
        }
        $parent = [IO.Directory]::GetParent($probe)
        if ($null -eq $parent) { break }
        $probe = $parent.FullName
    }
}

function Assert-SafeRelative([string]$Name) {
    if (-not $Name -or $Name.Length -gt 240 -or $Name.Contains('\') -or $Name.Contains(':') -or
        $Name.StartsWith('/') -or $Name -match '[\x00-\x1f<>"|?*]' -or
        $Name -match '(^|/)(\.|\.\.)(/|$)' -or $Name -match '[. ](/|$)' -or
        $Name -match '(?i)(^|/)(user_data|local_private|publisher_private|task_control|auth|auth_config|credentials|imports|exports|backups|ai_jobs|generated_images|\.codex|\.aws|\.git)(/|$)' -or
        $Name -match '(?i)\.(sqlite|sqlite3|db|dpapi|pem|key|pfx|p12|env)$' -or
        $Name -match '(?i)(^|/)(auth\.json|hosts\.yml|config\.toml|secrets\.json)$') {
        throw '설치 자료에 허용하지 않은 파일 경로가 있습니다.'
    }
}

function Get-ReviewedSourceFiles([string]$Root) {
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $found = New-Object 'System.Collections.Generic.List[System.IO.FileInfo]'
    $pending.Push($Root)
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        Assert-NoReparse $directory
        foreach ($item in @(Get-ChildItem -LiteralPath $directory -Force)) {
            $relativeName = $item.FullName.Substring($Root.Length + 1).Replace('\','/')
            Assert-SafeRelative $relativeName
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw '설치 자료의 링크 또는 연결 폴더를 거부했습니다.'
            }
            if ($item.PSIsContainer) { $pending.Push($item.FullName) }
            else { $found.Add($item) }
            if ($found.Count -gt 10000) { throw '공개 설치 파일 수의 한계를 넘었습니다.' }
        }
    }
    return $found.ToArray()
}

try {
    # Only Windows desktop 10/11 x64. No service, registry or global policy write.
    if ($env:OS -ne 'Windows_NT') { throw 'Windows 10 또는 11의 64비트 PC에서 실행하세요.' }
    Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Text;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using Microsoft.Win32.SafeHandles;
public static class AromaInstallNative {
  [StructLayout(LayoutKind.Sequential)] public struct SystemInfo {
    public ushort Architecture, Reserved; public uint PageSize;
    public IntPtr Min, Max, Mask; public uint Processors, Type, Granularity;
    public ushort Level, Revision;
  }
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)] public struct OsInfo {
    public uint Size, Major, Minor, Build, Platform;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=128)] public string Csd;
    public ushort SpMajor, SpMinor, Suite; public byte ProductType, Reserved;
  }
  [StructLayout(LayoutKind.Sequential)] public struct FileInfo {
    public uint Attributes; public System.Runtime.InteropServices.ComTypes.FILETIME Created, Accessed, Written;
    public uint Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
  }
  [DllImport("kernel32.dll")] static extern void GetNativeSystemInfo(out SystemInfo info);
  [DllImport("ntdll.dll", CharSet=CharSet.Unicode)] static extern int RtlGetVersion(ref OsInfo info);
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern SafeFileHandle CreateFileW(
    string path, uint access, uint share, IntPtr security, uint creation, uint flags, IntPtr template);
  [DllImport("kernel32.dll", SetLastError=true)] static extern bool GetFileInformationByHandle(SafeFileHandle handle, out FileInfo info);
  [DllImport("kernel32.dll", SetLastError=true)] static extern uint GetFileType(SafeFileHandle handle);
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern uint GetFinalPathNameByHandleW(
    SafeFileHandle handle, StringBuilder path, uint size, uint flags);
  static void Check(SafeFileHandle handle, string expected) {
    FileInfo info;
    if (handle.IsInvalid || !GetFileInformationByHandle(handle, out info) || info.Links != 1 ||
        (info.Attributes & 0x410) != 0 || GetFileType(handle) != 1) throw new IOException("Regular single-link file required.");
    StringBuilder final = new StringBuilder(32768);
    uint count = GetFinalPathNameByHandleW(handle, final, (uint)final.Capacity, 0);
    string name = final.ToString();
    if (name.StartsWith(@"\\?\")) name = name.Substring(4);
    if (count == 0 || count >= final.Capacity || !String.Equals(Path.GetFullPath(name), Path.GetFullPath(expected),
          StringComparison.OrdinalIgnoreCase)) throw new IOException("Opened path changed.");
  }
  static FileStream ReadChecked(string path) {
    SafeFileHandle handle = CreateFileW(Path.GetFullPath(path), 0x80000000, 1, IntPtr.Zero, 3, 0x00200080, IntPtr.Zero);
    try { Check(handle, path); return new FileStream(handle, FileAccess.Read); }
    catch { handle.Dispose(); throw; }
  }
  public static bool Supported() {
    SystemInfo system; GetNativeSystemInfo(out system);
    OsInfo version = new OsInfo(); version.Size = (uint)Marshal.SizeOf(typeof(OsInfo));
    return RtlGetVersion(ref version) == 0 && system.Architecture == 9 && version.Major == 10 &&
           version.Build >= 17763 && version.ProductType == 1;
  }
  public static string HashChecked(string path) {
    using (FileStream source = ReadChecked(path)) using (SHA256 hash = SHA256.Create())
      return BitConverter.ToString(hash.ComputeHash(source)).Replace("-", "").ToLowerInvariant();
  }
  public static void CopyChecked(string sourceName, string targetName, string expectedHash) {
    using (FileStream source = ReadChecked(sourceName)) {
      SafeFileHandle handle = CreateFileW(Path.GetFullPath(targetName), 0x40000000, 0, IntPtr.Zero, 1, 0x00200080, IntPtr.Zero);
      try {
        Check(handle, targetName);
        using (FileStream target = new FileStream(handle, FileAccess.Write)) { source.CopyTo(target); target.Flush(true); }
      } catch { handle.Dispose(); throw; }
    }
    if (!String.Equals(HashChecked(targetName), expectedHash, StringComparison.Ordinal)) throw new IOException("Copied hash changed.");
  }
}
'@
    if (-not [AromaInstallNative]::Supported()) { throw 'Windows 10/11 x64가 필요합니다. ARM 또는 32비트 PC에서는 설치하지 않았습니다.' }
    Assert-NoReparse $installerSource
    Assert-NoReparse $appSource
    if (-not (Test-Path -LiteralPath $appSource -PathType Container)) { throw 'ZIP 전체를 먼저 한 폴더에 압축 해제하세요.' }

    $manifestPath = Join-Path $appSource 'PUBLIC_MANIFEST.json'
    $scanPath = Join-Path $appSource 'PUBLIC_SCAN.json'
    [void][AromaInstallNative]::HashChecked($manifestPath)
    [void][AromaInstallNative]::HashChecked($scanPath)
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $scan = Get-Content -LiteralPath $scanPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.schema_version -ne 1 -or $scan.status -ne 'passed_static_staging' -or
        @($scan.blocking_findings).Count -ne 0 -or @($scan.unlisted_code_files).Count -ne 0 -or
        $scan.candidate_snapshot_unchanged -ne $true -or $scan.final_guides_unchanged -ne $true -or
        $scan.runtime_binaries_included -ne $true -or $scan.runtime_files_hash_verified -ne 45 -or
        $scan.catalog_programs -ne 0 -or $scan.file_index_items -ne 0) {
        throw '공개 설치 자료 검증 표시가 올바르지 않습니다. 검증된 ZIP을 다시 받으세요.'
    }
    $versionPath = Join-Path $appSource 'VERSION.json'
    [void][AromaInstallNative]::HashChecked($versionPath)
    $releaseVersion = Get-Content -LiteralPath $versionPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($releaseVersion.version -ne '1.1.0' -or $releaseVersion.schema_version -ne 2) {
        throw '1.1.0 설치 자료의 버전 표시가 올바르지 않습니다.'
    }
    $expected = @{}
    foreach ($record in $manifest.files) {
        $name = [string]$record.path
        Assert-SafeRelative $name
        if ($expected.ContainsKey($name) -or [string]$record.sha256 -notmatch '^[0-9a-f]{64}$' -or [long]$record.bytes -lt 0) {
            throw '공개 파일 목록이 올바르지 않습니다.'
        }
        $expected[$name] = @{ sha = [string]$record.sha256; bytes = [long]$record.bytes }
    }
    foreach ($name in @('PUBLIC_MANIFEST.json','PUBLIC_SCAN.json')) {
        $file = Join-Path $appSource $name
        $expected[$name] = @{ sha = [AromaInstallNative]::HashChecked($file); bytes = (Get-Item -LiteralPath $file).Length }
    }
    $actual = @(Get-ReviewedSourceFiles $appSource)
    if ($actual.Count -ne $expected.Count) { throw '설치 자료에 목록에 없는 파일이 있거나 필요한 파일이 빠졌습니다.' }
    foreach ($file in $actual) {
        $name = $file.FullName.Substring($appSource.Length + 1).Replace('\','/')
        Assert-SafeRelative $name
        Assert-NoReparse $file.FullName
        if (-not $expected.ContainsKey($name) -or $file.Length -ne $expected[$name].bytes -or
            [AromaInstallNative]::HashChecked($file.FullName) -ne $expected[$name].sha) {
            throw '파일 해시 검증에 실패했습니다. 원본 PC 자료는 변경하지 않았습니다.'
        }
    }
    $runtimePin = Get-Content -LiteralPath (Join-Path $appSource 'frozen_update\TRUSTED_RUNTIME.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $publisher = Get-Content -LiteralPath (Join-Path $appSource 'frozen_update\TRUSTED_PUBLISHER.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if (@($runtimePin.PSObject.Properties).Count -ne 3 -or @($publisher.PSObject.Properties).Count -ne 4 -or
        $runtimePin.schema_version -ne 1 -or $runtimePin.relative_executable -ne 'runtime/python/python.exe' -or
        $runtimePin.sha256 -ne '31fe10b4ed82a2f960af0ef9931f46a4b84ff286f8b6ffd9dee098cf4f10f17f' -or
        $publisher.algorithm -ne 'ecdsa-p256-sha256-p1363' -or
        $publisher.public_key_format -ne 'sec1-uncompressed-p256' -or
        $publisher.public_key_b64 -ne 'BBgn8fkd7z/1+wGpx/p7gJAEQjeTRV2nwZT0l1q0IryqPEKMyqb/Zss6MmAzaYkugOKjLhwFEGZHPxdGnjDl45I=' -or
        $publisher.fingerprint_sha256 -ne 'd46122579c50f43525528d2d69902b73be6c3aa8b0fa4d0a3f59ef1d98a27486') {
        throw '초기 실행기 신뢰 정보가 검증된 배포와 다릅니다.'
    }
    $catalog = Get-Content -LiteralPath (Join-Path $appSource 'content\catalog.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $index = Get-Content -LiteralPath (Join-Path $appSource 'content\files.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if (@($catalog.programs).Count -ne 0 -or @($index.files).Count -ne 0) { throw '초기 설치 자료가 빈 업무 자료 상태가 아닙니다.' }

    if ($TestTarget) {
        if (-not [IO.Path]::IsPathRooted($TestTarget)) { throw '검증 대상 경로는 절대 경로여야 합니다.' }
        $installTarget = [IO.Path]::GetFullPath($TestTarget).TrimEnd('\')
    } else {
        $userLocal = [Environment]::GetFolderPath('LocalApplicationData')
        if (-not $userLocal) { throw '현재 사용자의 설치 폴더를 확인할 수 없습니다.' }
        $installTarget = [IO.Path]::GetFullPath((Join-Path $userLocal 'HealingAromaWork_1_1_0'))
    }
    if (-not [IO.Path]::IsPathRooted($installTarget) -or $installTarget -eq [IO.Path]::GetPathRoot($installTarget).TrimEnd('\') -or
        $installTarget.Substring(2).Contains(':') -or $installTarget -match '[. ](\\|$)' -or
        $installTarget.StartsWith('\\') -or $installTarget.Length -gt 180 -or
        $installTarget.Equals($installerSource,[StringComparison]::OrdinalIgnoreCase) -or
        $installTarget.StartsWith($installerSource + '\',[StringComparison]::OrdinalIgnoreCase) -or
        (Test-Path -LiteralPath $installTarget)) {
        throw '대상 폴더가 이미 있거나 설치할 수 없는 경로입니다. 기존 앱과 자료는 덮어쓰지 않았습니다.'
    }
    Assert-NoReparse $installTarget
    $installParent = [IO.Directory]::GetParent($installTarget).FullName
    if (-not (Test-Path -LiteralPath $installParent -PathType Container)) {
        throw '설치 대상의 상위 폴더를 먼저 준비하세요.'
    }
    $ownedStage = Join-Path $installParent ('.aroma-install-' + [Guid]::NewGuid().ToString('N'))
    if (Test-Path -LiteralPath $ownedStage) { throw '새 설치 준비 폴더를 만들 수 없습니다.' }
    [void](New-Item -ItemType Directory -Path $ownedStage)
    $stagedApp = Join-Path $ownedStage 'aroma_app'
    [void](New-Item -ItemType Directory -Path $stagedApp)
    foreach ($name in $expected.Keys) {
        $inputFile = Join-Path $appSource $name.Replace('/','\')
        $outputFile = [IO.Path]::GetFullPath((Join-Path $stagedApp $name.Replace('/','\')))
        if (-not $outputFile.StartsWith($stagedApp + '\',[StringComparison]::OrdinalIgnoreCase)) { throw '설치 파일이 준비 폴더를 벗어났습니다.' }
        $directory = [IO.Directory]::GetParent($outputFile).FullName
        [void][IO.Directory]::CreateDirectory($directory)
        Assert-NoReparse $directory
        [AromaInstallNative]::CopyChecked($inputFile,$outputFile,$expected[$name].sha)
    }
    foreach ($name in @('INSTALL_APP.ps1','INSTALL.cmd','README.md','START_HERE.html')) {
        $inputFile = Join-Path $installerSource $name
        if (Test-Path -LiteralPath $inputFile -PathType Leaf) {
            Assert-NoReparse $inputFile
            $inputHash = [AromaInstallNative]::HashChecked($inputFile)
            [AromaInstallNative]::CopyChecked($inputFile,(Join-Path $ownedStage $name),$inputHash)
        }
    }
    Assert-NoReparse $ownedStage
    Assert-NoReparse $installParent
    if (Test-Path -LiteralPath $installTarget) { throw '설치 중 대상 폴더가 생겼습니다. 기존 폴더는 변경하지 않았습니다.' }
    $resolvedStage = [IO.Path]::GetFullPath($ownedStage)
    $resolvedTarget = [IO.Path]::GetFullPath($installTarget)
    if ([IO.Directory]::GetParent($resolvedStage).FullName -ne [IO.Directory]::GetParent($resolvedTarget).FullName -or
        -not $resolvedStage.StartsWith($installParent + '\.aroma-install-',[StringComparison]::OrdinalIgnoreCase)) {
        throw '설치 준비 폴더와 대상 경로 검증에 실패했습니다.'
    }
    # Move exactly our new sibling folder. No existing target, overwrite or cleanup.
    # Native .NET directory rename fails if a target appears during the last check.
    # Move-Item can instead nest the stage inside an existing target directory.
    [IO.Directory]::Move($resolvedStage,$resolvedTarget)
    $ownedStage = $null
    Write-Host '설치 완료: 공개 파일과 공식 런타임 해시를 모두 확인했습니다.'
    Write-Host ('앱 폴더: ' + $resolvedTarget)
    Write-Host 'AI 로그인·모델 요청·업데이트는 설치 중 실행하지 않았습니다.'
    if (-not $SkipShortcut) {
        try {
            $desktop = [Environment]::GetFolderPath('Desktop')
            $shortcutPath = Join-Path $desktop '아로마 업무 도우미 1.1.lnk'
            Assert-NoReparse $desktop
            if (Test-Path -LiteralPath $shortcutPath) {
                Write-Host '바탕화면 바로가기가 이미 있어 그대로 보존했습니다.'
            } else {
                $shell = New-Object -ComObject WScript.Shell
                $shortcut = $shell.CreateShortcut($shortcutPath)
                $shortcut.TargetPath = Join-Path $resolvedTarget 'aroma_app\START_APP.cmd'
                $shortcut.WorkingDirectory = Join-Path $resolvedTarget 'aroma_app'
                $shortcut.Description = '아로마 업무 도우미 1.1.0'
                $shortcut.Save()
                [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
                Write-Host '바탕화면의 아로마 업무 도우미 1.1을 실행하세요.'
            }
        } catch {
            Write-Host '바로가기는 만들지 못했습니다. 앱 폴더의 aroma_app\START_APP.cmd를 실행하세요.'
        }
    } else {
        Write-Host '검증 옵션으로 바로가기 생성을 생략했습니다.'
    }
    Write-Host '사용 안내: aroma_app\MOTHER_GUIDE.html / 관리 안내: aroma_app\SON_GUIDE.md'
    exit 0
} catch {
    Write-Host ('설치를 중단했습니다: ' + $_.Exception.Message)
    Write-Host '기존 앱·원본 자료를 삭제하거나 덮어쓰지 않았습니다.'
    if ($ownedStage -and (Test-Path -LiteralPath $ownedStage)) {
        Write-Host ('실패한 준비 폴더를 보존했습니다: ' + $ownedStage)
    }
    exit 1
}
