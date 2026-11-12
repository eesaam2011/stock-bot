# Step 3AO: fail closed for unhandled trade revisions

The live Step 3AN evidence includes an Alpaca SIP `T=x` trade cancel/error
after a verified reconnect ACK. This patch makes the epoch capture explicitly
invalidate and raise `SIP_TRADE_REVISION_UNRECONCILED` on `x` or `c`, rather
than silently ignoring the message and letting ordinary trades continue to
look complete. It also checks these types before the subscription ACK and
preserves post-ACK messages in a combined frame so the same guard applies.
The terminal diagnostic identifies `TRADE_REVISION_UNRECONCILED`, and the
read-only reconnect probe stops for safety. It does not classify a transport
exception as SIP error 407; only a SIP error frame can do that.

This is a containment step. It does not implement trade-ID correction/cancel
semantics, full-session coverage, an E/B reconstruction, or safe persistence.
The reviewed system remains in Draft and disallows DIRECT, Shadow, and
retroactive entries. The next bounded reconciliation design must account for
an original trade that was already ACKed, duplicates, pre-session originals,
and REST overlap without treating raw event count as proof of state parity.
