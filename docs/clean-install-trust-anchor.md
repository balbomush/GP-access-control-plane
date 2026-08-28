# Legacy clean-install root trust anchor

This is an operational boundary for the one-time legacy clean-install bridge.
It is not an installer, an update/rollback route, or a substitute for a
release signature.

Legacy `v0.3.4` and `v0.3.5-alpha.4` have no installed GP root helper that can
start the clean-remove action. Therefore the first use of the bridge requires
an operator at the board's **physical root console** to establish a fixed
trust anchor before the install user creates a vault. SSH, a user-owned
candidate cache, `queue-update`, a caller supplied path/URL/shell, and copied
files from `$HOME/.cache` are not a trust anchor and must stop the procedure.

## Preconditions

The operator must already have an organisation-approved, root-owned public key
and its independently recorded fingerprint, plus a signed immutable release
artifact that binds the exact candidate commit. This repository deliberately
does not provide a production public key, fingerprint, private key, or a
default value for any of them. If any of those values is absent or does not
verify, do not install an adapter and do not begin the bridge.

## Physical-console operation

After independently verifying the signed artifact and exact candidate commit,
the physical-console operator may install only these fixed files under
`/usr/local/libexec/gp-control-plane`, all as regular non-links:

| File | Owner/mode | Purpose |
| --- | --- | --- |
| `gp-clean-remove-adapter` | `root:root 0700` | Fixed dispatch only: `clean-remove --confirm-clean-remove`. |
| `gp-clean-remove-root` | `root:root 0700` | Existing fixed allowlist cleaner. |
| `gp-clean-remove-preflight` | `root:root 0755` | Install-user vault/handoff topology validation. |
| `gp-clean-remove-root.manifest` | `root:root 0600` | Existing five-line cleaner/preflight pin manifest. |
| `gp-clean-remove-adapter.manifest` | `root:root 0600` | Exact five-line adapter manifest below. |

The adapter manifest has exactly this ordered, newline-terminated format:

```text
install_user=INSTALL_USER
candidate_sha=40-lowercase-hex
adapter_sha256=64-lowercase-hex
cleaner_sha256=64-lowercase-hex
preflight_sha256=64-lowercase-hex
```

`INSTALL_USER` is fixed at anchor creation. The adapter checks its own path,
owner, mode and hash; both fixed payload hashes; and this strict manifest
before it dispatches the cleaner. The sudo policy must allow the install user
only the exact root-owned adapter command:

```text
/usr/local/libexec/gp-control-plane/gp-clean-remove-adapter clean-remove --confirm-clean-remove
```

No wildcard arguments, direct cleaner entry, generic shell, path, URL, Git
operation, service command, snapshot, rollback, updater, or vault/handoff
input is permitted. The operator records only a sanitized success/failure
verdict; no vault, handoff, archive, secret, state, or private key is copied
to or from another board.

The first clean-remove must still be preceded by the unprivileged bridge's
exact canonical legacy-state validation and successful vault/handoff
preflight. A failed anchor check is a stop-before-vault/root-action condition;
it is not repaired by creating a cache or by rerunning a legacy installer.

## Pi2 signed-evidence trust anchor

Before any Pi2 fixture, cache, vault, clean-remove or target installation, the
same physical-console process must install the independently approved public
key, its exact SHA-256 pin, and the reviewed verifier at these fixed paths:

| File | Owner/mode |
| --- | --- |
| `/etc/gp-control-plane/trust/pi5-evidence-ed25519.pub` | `root:root 0644` |
| `/etc/gp-control-plane/trust/pi5-evidence-ed25519.pub.sha256` | `root:root 0644` |
| `/usr/local/libexec/gp-control-plane/verify-pi5-evidence-bundle` | `root:root 0755` |

The pin file contains only the newline-terminated lower-case SHA-256 of the
public-key bytes. The verifier has no caller-selected key or evidence path; it
accepts only exact C and validates the fixed signed canonical bundle. Missing,
wrong-C, incomplete, non-canonical, unsigned or invalidly signed evidence is a
stop-before-cache/fixture/vault/remove condition. The private signing key is
never placed on Pi2, Pi5, the repository, or the release ledger.
