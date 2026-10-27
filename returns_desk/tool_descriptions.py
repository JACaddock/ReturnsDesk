"""Every word the model reads about a tool lives here.

Treat edits to this file like code changes: bump VERSION, and let the eval suite
tell you whether the change helped. VERSION is written into every transcript so
eval results can be traced back to the descriptions that produced them.
"""

VERSION = "6"

# Shared by both write tools. "Call first" stops the model asking for a yes itself (NOTES.md, milestone 2).
_CONFIRMATION = (
    "Call this tool as soon as you know the details, before asking the customer anything: don't ask them to "
    "confirm yourself. The first call does nothing except return the exact action to put to the customer. "
    "Then ask them to reply yes, and after they do, call the tool again with the same arguments."
)

VERIFY_CUSTOMER = (
    "Confirm who the customer is. Needs the order number (like A1043) and the email address on that order, "
    "both from the customer; ask for whichever is missing rather than guessing. Every other order tool fails "
    "until this succeeds. On success it returns the customer's first name and all their order numbers."
)
VERIFY_CUSTOMER_PARAMS = {
    "order_id": "An order number exactly as the customer gave it, for example A1043.",
    "email": "The email address the customer says they used for the order.",
}

GET_ORDER = (
    "Look up one of the verified customer's orders. Returns the status, dispatch and delivery dates, carrier, "
    "tracking number, the items (with the item_id you need for start_return), the order total and how much "
    "has already been refunded."
)
GET_ORDER_PARAMS = {
    "order_id": "One of the order numbers returned by verify_customer.",
}

SEARCH_POLICY = (
    "Search the returns and refunds policy. Use it before answering any question about what the policy "
    "allows. Returns up to 3 matching subsections, best first, each with its section number (like 3.2); "
    "answer only from their text and cite the section number, for example \"(section 3.2)\". If it returns "
    "no matches, the policy doesn't cover the question: say so instead of guessing."
)
SEARCH_POLICY_PARAMS = {
    "query": "The customer's question in your own words, for example 'can I return opened headphones'.",
}

START_RETURN = (
    "Start a return for one item on a verified customer's order. The tool checks the 30-day window, "
    "final-sale items and duplicate returns itself. " + _CONFIRMATION
)
START_RETURN_PARAMS = {
    "order_id": "The order the item is on.",
    "item_id": "The item_id from get_order.",
    "reason": "Why the customer is returning it, in a few words.",
}

ISSUE_REFUND = (
    "Refund money on a verified customer's delivered order. For orders that are processing, shipped or "
    "cancelled, use escalate_to_human. Not for an item being returned: that refund is paid when it "
    "arrives back. Refunds totalling up to £75 per order can be issued here; "
    "for anything more, use escalate_to_human instead, and never split a refund into smaller amounts to get "
    "under the limit. " + _CONFIRMATION
)
ISSUE_REFUND_PARAMS = {
    "order_id": "The order to refund.",
    "amount_pence": "The amount in pence, for example 2499 for £24.99.",
    "reason": "Why the refund is being given, in a few words.",
}

ESCALATE = (
    "Pass the conversation to a member of the team. Use it when the customer asks for a person, when a "
    "refund is over £75, when a request needs a judgement call the policy leaves to the team, or when you "
    "can't make progress. After this, write one short reply telling the customer what happens next."
)
ESCALATE_PARAMS = {
    "reason": "What the team needs to know, in one or two sentences.",
    "order_id": "The order it's about, or null if there isn't one.",
}
