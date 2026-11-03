# Security policy

## Supported versions

Only the newest release is supported. Releases are cut automatically from
`main`, and a fix ships in the next one. Older tags are not rebuilt, so moving
to the newest image is how you get a fix.

## Reporting a vulnerability

Please report it privately, not in a public issue: use **Report a
vulnerability** on the
[Security tab](https://github.com/jarrodruggiero/opsbox/security/advisories/new).

What happens next:

- I reply in the report, and work out the fix with you there.
- The fix is written in a private fork, so nothing public describes the problem
  before a fix exists.
- When the release with the fix is out, I publish the advisory and credit you,
  unless you would rather not be named.

Please give me a reasonable window to fix it before disclosing it anywhere
else.

## What counts

A flaw in what this repository builds: the image, its entrypoint and scripts,
and the workflows that build and publish it. For example, a tool installed
without its signature or checksum being checked, a workflow that runs code from
a pull request with write access, or the image doing something as root it does
not need to.

Two things are not this repository's:

- A vulnerability in a tool the image carries (Claude Code, kubectl, Terraform
  and the rest). Please report it to that project. The weekly release picks up
  its fix once the project ships it.
- What you allow Claude Code to do inside the box. The image keeps your keys
  in your volume and drops every privilege it can, but anything with your
  kubeconfig can do what your kubeconfig can do.
