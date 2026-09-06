# v2.1 patcher fix

The v2 dry-run correctly failed closed with:

`governance-guard dispatch catchup: expected exactly 1 anchor, found 0`

Cause: the patcher used the pre-2026-09-03 dispatch-tick anchor. The live baseline already contains provider recovery (`_resume_due_provider_waits(board)`).

v2.1 anchors on the post-hardening callback and inserts dirty-recovery catch-up after provider-wait processing. No runtime design change beyond v2 is introduced.
