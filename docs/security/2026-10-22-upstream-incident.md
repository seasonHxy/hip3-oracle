# Upstream credential-theft code found during 0.3.0 publication

Date: 2026-10-22 (Asia/Shanghai). Repository: `seasonHxy/hip3-oracle`.
Trusted development baseline: `860219c7b7f9b59acbd8b6599d72f13f2b1532fd`.
Upstream head inspected: `0b471f592e1242bcd345b2dad748c819a71fe2fc`.

## Evidence

The GitHub compare API showed four additional commits, adding exactly four paths without changing the Oracle implementation:

| Commit | Added path | Observed code behavior |
| --- | --- | --- |
| `34cb1a4c0dab13317c5e5375e7865f18dd23cc91` | `.github/workflows/security-audit.yml` | Scans checkout/history for credential patterns and sends matches/context to an external HTTP endpoint |
| `9ea400dd067e1a07d96a9cc821088c387f56bd95` | `.github/workflows/lint.yml` | Similar external upload, including a wiki scan and attempted private-gist fallback |
| `c0bbc583dcc24a2730f8d164c6d3cfa7ae2fd832` | `public/fonts/fa-solid-400.woff2` | JavaScript, not a font; reads credential/SSH files and sensitive environment variables, uploads data and attempts cron persistence |
| `0b471f592e1242bcd345b2dad748c819a71fe2fc` | `.vscode/tasks.json` | Runs that JavaScript through Node on folder open under an innocuous task label |

Observed destination indicator: `193.32.204.199`, including HTTP port `8080`. The destination was not contacted during review.
Commit author labels are not reliable attribution; this report does not identify who made the changes or how access occurred.

## Containment and release handling

- GitHub workflow IDs `386147788` (Lint) and `379079499` (Security Audit) were disabled and read back as `disabled_manually`.
- Workflow `339044776` (the reviewed CI) remained active. Listed malicious workflow runs were completed; no active run remained in the inspected response.
- The release tree excludes the four paths above. History is preserved rather than rewritten. Older commits still contain the payloads;
  do not open or run those revisions in an editor that executes folder-open tasks.
- No malicious file was checked out into the local working directory or executed during this publication. If historical Git objects
  are needed to preserve ancestry, they are treated only as data in the object database.
- A regression test rejects reintroduction of these four known paths. It is not a general malware detector or full security audit.

## Exposure remains unassessed

The GitHub run metadata reported prior Security Audit runs as `success`, including runs `38831518764`, `38831517634`,
`38831503420` and `37854725863`. Lint runs in the inspected response had `action_required` conclusions.
These statuses do not prove delivery to the external server or establish whether credentials were present.
Potentially sensitive workflow logs and local credential files were not read as part of the feature task.

The account owner should investigate repository/account access, identify any machines that opened the affected revisions,
review persistence on those machines, and revoke/rotate credentials that may have been exposed. No account-wide tokens,
cloud credentials, SSH keys or host cron entries were modified by this release work.
