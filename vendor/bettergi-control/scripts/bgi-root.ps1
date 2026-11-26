# 来自上游 bettergi-control，按便携目录解析引擎根路径。
function Resolve-BgiRoot {
    [CmdletBinding()]
    param([string]$Root = '')
    if (!$Root) { $Root = $env:BETTERGI_ROOT }
    if (!$Root) {
        $project = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
        $bundle = (Get-Content -LiteralPath (Join-Path $project 'bundles\manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json).components.bettergi
        $name = 'bettergi-{0}-{1}' -f $bundle.version,$bundle.sha256.Substring(0,12)
        $Root = Join-Path $project (Join-Path '.runtime' (Join-Path $name 'BetterGI'))
    }
    if ([IO.Path]::GetExtension($Root) -eq '.exe') { $Root = Split-Path -Parent $Root }
    return [IO.Path]::GetFullPath($Root)
}
