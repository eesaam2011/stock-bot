# Step 3AZ — automatic terminal evidence handoff to recovery

The actual production composition disconnect callback now passes the runtime's pre-teardown revision diagnostic to a bounded recovery inbox, after trust/cache invalidation. This is memory-only: no REST call, Redis write or new asynchronous task occurs inside the decision lock/socket callback.

One <=16 KiB serialized diagnostic is retained with defensive copies. Epoch/digest mismatch or oversized input blocks replay. Exact repeat is idempotent. A second distinct unresolved revision permanently blocks this inbox rather than overwriting evidence. Later non-revision disconnects do not erase the retained diagnostic.

ProductionStartupRecovery.preview_pending_revision uses the retained diagnostic through the existing native replay candidate path and checks that the inbox remains unchanged afterward. Concurrent second revisions reject the candidate. Preview does not consume the evidence, clear the blocker, restore readiness or authorize writes.

This is automatic evidence routing only, NOT automatic recovery scheduling or successful recovery. The inbox is not durable across process restart. A bounded multi-revision reconciliation protocol and independent correction-incorporation proof remain required. No new live session. Default startup still fails closed. No Render changes, main merge, DIRECT or Shadow activation.

Tests cover the actual composed disconnect callback, subsequent EOF preservation, defensive copying, exact duplicates, conflicting revisions, malformed/oversized handoff, native candidate preview, and a second revision arriving during REST.
