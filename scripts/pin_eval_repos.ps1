# Prints the latest commit hash (HEAD of the default branch) for each eval repo.
# No cloning needed. Paste the output into docs/eval-repos.md.
$repos = @(
  "https://github.com/pallets/click",
  "https://github.com/psf/requests",
  "https://github.com/pallets/flask",
  "https://github.com/Textualize/rich",
  "https://github.com/encode/httpx"
)
foreach ($url in $repos) {
  $line = git ls-remote $url HEAD
  $sha = ($line -split "\s+")[0]
  $name = Split-Path $url -Leaf
  "{0}  {1}  {2}" -f $name, $sha, $url
}
