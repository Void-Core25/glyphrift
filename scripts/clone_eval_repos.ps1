# Clones the evaluation repos listed in docs/eval-repos.md into eval_repos\ and checks out the
# pinned commits. Safe to run again: existing clones are reused and re-checked.
$root = Split-Path -Parent $PSScriptRoot
$doc = Get-Content (Join-Path $root "docs\eval-repos.md")
$rows = $doc | Select-String -Pattern '^\|\s*(\w+)\s*\|\s*`([0-9a-f]{40})`\s*\|\s*(https://\S+)\s*\|'
if (-not $rows) { throw "No pinned repos found in docs/eval-repos.md" }

foreach ($row in $rows) {
  $name = $row.Matches[0].Groups[1].Value
  $commit = $row.Matches[0].Groups[2].Value
  $url = $row.Matches[0].Groups[3].Value
  $dest = Join-Path $root "eval_repos\$name"

  if (-not (Test-Path $dest)) {
    Write-Host "Cloning $name ..."
    git clone --quiet $url $dest
    if ($LASTEXITCODE -ne 0) { throw "git clone failed for $name" }
  }
  git -C $dest -c advice.detachedHead=false checkout --quiet $commit
  if ($LASTEXITCODE -ne 0) { throw "could not check out $commit in $name" }
  $head = (git -C $dest rev-parse HEAD).Trim()
  if ($head -eq $commit) { Write-Host ("OK       {0,-9} {1}" -f $name, $head) }
  else { Write-Host ("MISMATCH {0,-9} {1}" -f $name, $head) }
}
