# IdeaPress — security

IdeaPress holds your briefs, your drafts and your source material. It is designed so that none of
it leaves the machine unless you ask, and so that model output — which is untrusted input, however
it was produced — cannot act.

## The bind

**Loopback by default**, port 8767, no authentication. That is safe only because nothing off the
machine can reach it.

Exposing it to a network is a deliberate act with two required steps:

```toml
[server]
host = "192.168.1.5"
allowed_hosts = ["ideapress.local"]     # required; without it, IdeaPress refuses to start
```

`allowed_hosts` is not decoration. Without a `Host` allowlist, any page you visit can make your
browser issue requests to a service on your own machine, and the DNS name it used is the only thing
distinguishing that from your own tab. The `Host` header is validated **before routing**, so a
request for a path that does not exist is still refused with 421 rather than 404 — the check cannot
be skipped by asking for something else.

Binding to `0.0.0.0` needs `allow_lan_exposure = true` on top, as a separate acknowledgement.

**Terminate TLS in front of it.** The CSRF cookie is `__Host-`-prefixed and `Secure`; on a plain
HTTP non-loopback origin a browser will not store it and form posts will be refused. That is the
flag working, not a defect.

## Model output

Untrusted, always, and treated as such:

* **Never executed.** Nothing in IdeaPress calls `eval`, `exec` or a subprocess, and a test asserts
  that over the whole source tree.
* **Never used to build a path.** A path-traversal string in model output is a *blocking*
  validation failure — the unit does not commit. It is the only payload treated that way; a
  `<script>` tag is flagged and rendered harmlessly, because an article about web security
  legitimately contains one.
* **Never rendered unescaped.** Every template escapes; no template applies `| safe`; no module
  builds a template from a runtime string. The HTML export escapes independently, in its own module,
  rather than trusting that something upstream did.
* **Never a control-flow decision.** A model cannot end a stage, satisfy a check-less requirement by
  silence, choose the next unit, or set its own budget.

The check that these hold is a **sweep**: the surfaces are enumerated from the code — export formats
from the registry, templates by walking the tree, pages from the routers — so a format or page added
later is covered the day it appears. The named failure mode for this kind of work is a gap in
exactly one surface.

## Your content

* Stored locally. Never uploaded.
* **Never logged at INFO or above.** `logging.include_content` is off by default; turning it on logs
  prompts and drafts, and is for debugging your own machine.
* A remote backend is opt-in (`providers.allow_remote`) and **labelled as egress** — on the backends
  page and in the workspace, where you are about to press the button.

## Archives

`ideapress project import` validates before it writes anything:

* no absolute paths, no `..` components;
* no symlinks or hardlinks, no device files;
* caps on entry count, per-entry size, total size and compression ratio.

A refused archive leaves no directory and no row. `--inspect` reports what an archive contains
without importing it, which is the thing to run first on one somebody sent you.

## Reporting something

See [SECURITY.md](../SECURITY.md) in the repository root.

## What IdeaPress does not do

* No authentication or user accounts. It is a single-user local application; a shared deployment
  needs a reverse proxy that authenticates in front of it.
* No sandboxing of model output, because it never executes any.
* No outbound network at all in the default configuration — the full test suite passes in a network
  namespace with no interfaces.
