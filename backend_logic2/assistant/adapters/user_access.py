"""Read-only authority boundary. Never infer roles from a chat or browser hint."""

def may_approve_po(actor: str) -> bool:
    from backend_logic2.policies.access import read_policy_access, can_approve_po
    return can_approve_po(read_policy_access(actor))
