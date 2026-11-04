# Android RPC canary baseline

**Last Updated:** 2026-10-28

`tests/fixtures/android/canary_baseline.json` records the response shapes and
unknown-field counts observed by `scripts/android_grpc_canary.py` against the
immutable CI template. Three consecutive protected-main RPC health runs agree on the current shapes:

- [2026-10-26](https://github.com/teng-lin/notebooklm-py/actions/runs/40285814067) (slot B)
- [2026-10-27](https://github.com/teng-lin/notebooklm-py/actions/runs/40466060375) (slot A)
- [2026-10-28](https://github.com/teng-lin/notebooklm-py/actions/runs/40647960597) (slot B)

| RPC | Structural SHA-256 | Unknown fields |
| --- | --- | --- |
| `GetProject` | `fd102fffd98d78658e0579edec8c6930ad5494f4bc9b38ad4b77af2a978c1b10` | 145 |
| `ListChatSessions` | `e6d6424e0fbcb1e967dba83ed719abf94fb78007b4d58dbc86c2b5c88756bc98` | 2 |

In all three runs, opening the Android session, refreshing the bearer, reading
the project ID, and listing the template's chat sessions succeeded. The only
schema mismatch was `GetProject`: its populated-field fingerprint changed from
`367f83fb36bb3bc67b174baad75bebad298e40319e27ea03afed1cf91e1cda37`
and its unknown-field count increased from 143 to 145. `ListChatSessions`
remained unchanged. The repeated observations across both account slots support
updating the structural baseline for v0.8.6; they do not identify the two new
unknown fields or imply complete recovered-schema coverage. The baseline was
updated after reviewing these diagnostics, without changing the strict canary
comparison or the protobuf decoder.

The fixture contains only structural hashes and counts. The canary continues to
fail on either kind of drift, a missing RPC entry, or a missing baseline. Review
new live diagnostics before changing these values; do not regenerate the
baseline automatically to clear a failing gate.
