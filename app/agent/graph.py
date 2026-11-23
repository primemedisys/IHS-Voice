"""
app/agent/graph.py

Inbound call graph: emergency check, identity, then conversation.
"""

from langchain_core.messages import AIMessage, ToolMessage # type: ignore
from langgraph.graph import END, START, StateGraph # type: ignore
from langgraph.prebuilt import ToolNode, tools_condition # type: ignore

from app.agent.booking import flow
from app.agent.booking.nodes import booking_flow, offer_booking
from app.agent.identity.nodes import collect_new_identity, verify_existing_identity
from app.agent.shared.loader import pick_reply
from app.agent.intake.nodes import (
    MAX_VERIFY_ATTEMPTS,
    assistant,
    escalate,
    lookup_patient,
    urgency_check,
)
from app.agent.state import CallState
from app.agent.intake.tools import TOOLS


# ==================================================
# SMALL NODES
# ==================================================

def verification_locked(state: CallState) -> dict:
    """Keep unverified callers out of the normal patient conversation."""
    reply = (
        "identity_refused"
        if state.get("identity_status") == "refused"
        else "verify_failed"
    )
    return {"messages": [AIMessage(pick_reply(reply))]}


# ==================================================
# ROUTING (which step comes next)
# ==================================================

def route_after_urgency(state: CallState) -> str:
    """Emergency first, followed by the appropriate identity path."""
    if state.get("turn_level") == "emergency":
        return "escalate"

    if state.get("caller_type") == "existing" and not state.get("is_verified"):
        if (
            state.get("verify_attempts", 0) >= MAX_VERIFY_ATTEMPTS
            or state.get("identity_status") in ("failed", "refused")
        ):
            return "verification_locked"
        return "verify_existing_identity"

    if state.get("caller_type") == "new":
        if state.get("identity_status") == "refused":
            return "verification_locked"
        if state.get("identity_stage") != "complete":
            return "collect_new_identity"

    return route_conversation(state)


def route_conversation(state: CallState) -> str:
    """Intake questions go to the LLM; once intake is done, code runs the booking flow."""
    if state.get("phase") in flow.BOOKING_PHASES:
        return "booking_flow"
    return "assistant"


def route_after_new_identity(state: CallState) -> str:
    if state.get("identity_stage") == "complete":
        return route_conversation(state)
    return END


def route_after_assistant(state: CallState) -> str:
    """Tool call -> run it. A plain reply -> speak it, unless the caller keeps
    dodging the intake questions for too long."""
    if tools_condition(state) == "tools":
        return "tools"
    if state.get("intake_turns", 0) >= flow.MAX_INTAKE_TURNS:
        return "offer_booking"
    return END


def route_after_tools(state: CallState) -> str:
    """When the saved intake is complete, the fixed booking offer is spoken
    instead of another LLM reply."""
    for message in reversed(state["messages"]):
        if isinstance(message, ToolMessage):
            if getattr(message, "name", "") == "request_callback" and "Intake complete" in str(message.content):
                return "offer_booking"
            continue
        if isinstance(message, AIMessage):
            break
    return "assistant"

# ==================================================
# BUILDING THE GRAPH
# ==================================================

def build_graph(checkpointer):
    """Build the inbound call graph: urgency check, identity, intake with tools, then the booking phases."""
    builder = StateGraph(CallState)

    builder.add_node("lookup_patient", lookup_patient)
    builder.add_node("urgency_check", urgency_check)
    builder.add_node("escalate", escalate)
    builder.add_node("verify_existing_identity", verify_existing_identity)
    builder.add_node("collect_new_identity", collect_new_identity)
    builder.add_node("verification_locked", verification_locked)
    builder.add_node("assistant", assistant)
    builder.add_node("tools", ToolNode(TOOLS))
    builder.add_node("offer_booking", offer_booking)
    builder.add_node("booking_flow", booking_flow)

    builder.add_edge(START, "lookup_patient")
    builder.add_edge("lookup_patient", "urgency_check")
    builder.add_conditional_edges(
        "urgency_check",
        route_after_urgency,
        [
            "escalate",
            "verify_existing_identity",
            "collect_new_identity",
            "verification_locked",
            "assistant",
            "booking_flow",
        ],
    )
    builder.add_edge("escalate", END)
    builder.add_edge("verify_existing_identity", END)
    builder.add_edge("verification_locked", END)
    builder.add_conditional_edges(
        "collect_new_identity",
        route_after_new_identity,
        ["assistant", "booking_flow", END],
    )

    builder.add_conditional_edges(
        "assistant",
        route_after_assistant,
        ["tools", "offer_booking", END],
    )
    builder.add_conditional_edges(
        "tools",
        route_after_tools,
        ["assistant", "offer_booking"],
    )
    builder.add_edge("offer_booking", END)
    builder.add_edge("booking_flow", END)

    return builder.compile(checkpointer=checkpointer)