# Shared root-owned run-ledger authority

| Field | Value |
|---|---|
| API | `smateway_global_run_ledger_v1` |
| Status | Implemented and offline-tested; host provisioning has **not** been performed |
| Runner identity | Provisioned explicitly, expected `pi` on devpi |
| Production mutation path | `/usr/bin/sudo -n -- /usr/local/libexec/smateway-ledger-helper OPERATION` |
| Caller-controlled authority paths | None |

## Purpose

Authoritative RF runs must remain consumed even if a caller deletes or restores its normal
state directory. A plan, manifest, or local tombstone beneath `~/.local/state/smateway` cannot
provide that property by itself because the runner owns that tree.

The shared ledger adds a separate OS-enforced authority. Its root, seal, helper, and sudo policy
are root-owned and not writable or removable by the unprivileged runner. A run namespace is
reserved before a prepared manifest is accepted, and its one-shot guard is consumed before any
frozen source, dependency, fixture, or hardware access. Hard-link anchors preserve run history
outside each ledger directory and prevent a deleted/restored local snapshot from making a run ID
usable again.

This authority prevents replay and accidental duplicate execution. It does not make RF data
scientifically valid; normal source, fixture, continuity, headroom, mute, and analysis admission
gates still apply.

## Fixed production layout

| Path | Owner/mode | Role |
|---|---|---|
| `/var/lib/smateway/global-run-ledger-v1` | `root:root 0755` | Authority root |
| `.../run-ledgers` | `root:root 0755` | Per-namespace monotonic ledgers |
| `.../inode-anchors` | `root:root 0755` | Durable hard-link history |
| `/etc/smateway/global-run-ledger-root-v1.json` | `root:root 0444` | Sealed identities, hashes, policies, runner UID/GID, and local device |
| `/usr/local/libexec/smateway-ledger-helper` | `root:root 0555` | Standard-library-only privileged helper |
| `/etc/sudoers.d/smateway-ledger-helper` | `root:root 0440` | Four-command `NOPASSWD` allowlist for the sealed runner |

Prepared reservation, guard, and emergency-failure slots are root-owned mode `0644`; sealing
changes them to `0444`. The execution marker is created once at mode `0444`. The helper uses
directory file descriptors, `O_NOFOLLOW`, exact inode/device/owner/mode checks, `O_EXCL`, hard-link
anchors, and file plus directory `fsync` operations.

The root seal binds the exact helper, sudo binary, and sudoers-policy hashes; the policy registry;
the runner user/UID/GID; the authority directory inodes; and the local filesystem device. A
different helper, policy, identity, device, path, or symlink fails closed.

## Allowed lifecycle

```text
unseen namespace
    |
    | reserve_run (O_EXCL directory + 3 slots + 3 hard-link anchors)
    v
prepared
    |
    | seal_slot(reservation)
    v
reserved and sealed
    |
    | consume_guard                    seal_slot(failure)
    v                                  (emergency failure path)
guard consumed  ------------------------------> permanently failed
    |
    | create_immutable_json(execution)
    v
permanently consumed
```

The exact operations permitted by sudoers are `reserve_run`, `seal_slot`, `consume_guard`, and
`create_immutable_json`. The helper independently reconstructs the campaign policy, namespace,
canonical run identity, ledger key, and destination paths. It rejects extra request fields,
unknown policies, malformed identities, incorrect transition order, unbound receipt documents,
and unexpected ledger inventory.

An interruption after guard consumption but before marker creation is still a burned run. The
runner attempts to seal the reserved emergency failure slot. If even that cleanup fails, the
nonempty immutable guard and its hard-link anchor still prevent reuse.

## Provisioning procedure

Provisioning is an explicit host-administration action. It does not flash firmware, access the
Pluto, transmit RF, or alter captures. Before provisioning, review the exact source revision and
require a clean worktree:

```bash
cd /home/pi/smateway
git status --short
git diff --check
```

Then install once for the unprivileged `pi` runner:

```bash
sudo -- /usr/bin/python3 \
  /home/pi/smateway/scripts/provision_smateway_global_ledger.py \
  --runner-user pi
```

The provisioner validates its generated sudoers rule with `visudo`, installs only the fixed paths
above, writes the root seal last, and immediately verifies the result. Existing files are accepted
only when their bytes, root ownership, group, and mode match exactly. A differing existing helper,
sudoers file, or seal is never overwritten. Such a mismatch requires a separately reviewed
migration; do not delete or replace authority files merely to make provisioning pass.

No provisioning command in this document has been run as part of the implementation or test
work. The tests use private, explicitly injected temporary storage and cannot be selected by a
production CLI.

## Read-only verification

Run this as the provisioned unprivileged runner:

```bash
/usr/bin/python3 \
  /home/pi/smateway/scripts/verify_smateway_global_ledger.py
```

The verifier performs no writes. It checks the sealed runner identity, fixed paths, no-symlink
ancestry, root ownership/group and modes, local device, helper/sudo/sudoers hashes, exact sudoers
content, policy-registry hash, and complete root-seal reconstruction. A nonzero exit status blocks
planning and execution.

Do not test the mutation helper by sending handcrafted production requests. The campaign runners
create authority-bound requests and validate exact privileged responses. Offline unit tests use
`LocalLedgerBackend` with `provision_local_test_storage(...)`; this adapter is explicitly marked
non-authoritative and is rejected by production storage validation.

## Enrolled campaigns

| Policy ID | Namespace identity |
|---|---|
| `p2-5g8-input-off-v1` | board + run ID |
| `t7-5g8-fine-frequency-v1` | board + run ID |
| `t6-5g8-port-pair-matrix-v1` | board + campaign + cell + repeat + run ID |

Each policy fixes its exact namespace and canonical-identity fields in source. Adding or changing
a policy changes the sealed registry and helper hash, so it requires a reviewed authority
migration before production use.

## Operator interpretation

- `verified` means the OS trust boundary matches its seal; it does not mean a run is prepared.
- A reservation failure saying a namespace already exists means that run identity is spent.
- A consumed guard with no marker indicates an interrupted burn transition, not a reusable run.
- A failure receipt records fail-closed cleanup; it is never an accepted artifact.
- Restoring local `plan.json`, `manifest.json`, or tombstones cannot restore an externally spent
  run identity.
- Never remove ledger directories or anchors during campaign recovery. Choose a new run ID and
  preserve the failed evidence for diagnosis.

## Offline verification completed

The focused tests cover the monotonic happy path, emergency failure after the guard/marker gap,
namespace and identity exactness, authority and receipt forgery, symlink rebound, path escape,
prepared-snapshot replay, deleted local tombstones, removed run directories, response forgery,
capture-root rebound, finalization quarantine, and exact noninteractive sudo invocation. The
provisioner has separate tests for root gating, fixed paths/modes, create-once refusal, and the
narrow sudoers allowlist. These tests do not mutate `/var`, `/etc`, or `/usr/local`.
