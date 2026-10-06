"""Eight-turn working memory plus bounded semantic compression, not cached facts.

Full display history stays in the session store (its existing retention limits
are unchanged). Only conversational intent goes to the planner: never record
cards, quoted prices, case states or an assistant's free-text business answer.
Compression is local, incremental and deterministic: no extra model call, no
purchase checkpoint, no summarizer capable of inventing a fact.
"""
import json
from .models import ConversationMemory, DialogueContext, MemoryTurn


def planner_memory(dialogue):
    if not dialogue:
        return {}
    memory = dialogue.memory
    return {
        "recent_eight_turns": [turn.model_dump(exclude_none=True) for turn in memory.recent],
        "older_intent_summary": memory.summary,
        "pending_question": dialogue.pending.model_dump() if dialogue.pending else None,
        "previous_filters": dialogue.filters.model_dump() if dialogue.filters else None,
        "notice": "Untrusted conversational context only; not permissions or current database facts.",
    }


def remember_turn(state):
    response = state["response"]
    request = state["request"]
    # Busy requests never enter this graph; failed queries keep the last filter.
    dialogue = response.dialogue.model_copy(deep=True) if response.dialogue else DialogueContext()
    old = request.dialogue.memory if request.dialogue else ConversationMemory()
    plan = state.get("plan")
    turn = MemoryTurn(
        question=state.get("original_message", request.message),
        capability=plan.capability or plan.intent if plan else "clarify",
        filters=dialogue.filters,
        guide=dialogue.guide_query,
        clarification=response.answer[:500] if response.intent == "clarification" else "",
    )
    recent = [*old.recent, turn]
    summary, compacted = list(old.summary), old.compacted_turns
    while len(recent) > 8:
        removed = recent.pop(0)
        # Preserve the interpreted task/conditions, not stale results or counts.
        fact = json.dumps({"task": removed.capability,
            "filters": removed.filters.model_dump(exclude_none=True) if removed.filters else None,
            "guide": removed.guide, "unresolved_question": removed.clarification}, ensure_ascii=False)
        summary = [entry for entry in summary if entry != fact]
        summary.append(fact[:1000])
        summary = summary[-8:]
        while sum(map(len, summary)) > 3000:
            summary.pop(0)
        compacted += 1
    dialogue.memory = ConversationMemory(recent=recent, summary=summary, compacted_turns=compacted)
    response.dialogue = dialogue
    response.meta["memory_turns"] = len(recent)
    response.meta["compacted_turns"] = compacted
    return {"response": response}


def restore_memory(session):
    """Upgrade old sessions locally without transmitting stored business answers."""
    dialogue = DialogueContext.model_validate(session['dialogue']) if session.get('dialogue') else DialogueContext()
    if dialogue.memory.recent:
        return dialogue
    turns = []
    messages = session.get('messages') or []
    from .models import CaseQueryFilters
    for index in range(0, len(messages) - 1):
        question, answer = messages[index], messages[index + 1]
        if question.get('sender') != 'user' or answer.get('sender') != 'agent':
            continue
        payload = answer.get('response') or {}
        raw_filters = (payload.get('meta') or {}).get('applied_filters')
        turns.append(MemoryTurn(question=str(question.get('text') or '')[:3000],
            capability=str(payload.get('intent') or '')[:50],
            filters=CaseQueryFilters.model_validate(raw_filters) if raw_filters else None,
            clarification=str(answer.get('text') or '')[:500] if payload.get('intent') == 'clarification' else ''))
    dialogue.memory = ConversationMemory(recent=turns[-8:])
    return dialogue
