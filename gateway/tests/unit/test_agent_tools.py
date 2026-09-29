import pytest
from pydantic import ValidationError

from app.services.agent_tools import AgentConfigInput, SubagentDefinitionInput, effective_tools, validate_rag_binding


def test_legacy_tools_and_explicit_empty_selection():
    assert effective_tools(None, False) == ["read", "bash", "edit", "write"]
    assert effective_tools(None, True) == ["read", "bash", "edit", "write", "rag_search"]
    assert effective_tools([], True) == []


def test_subagents_require_selected_tool_and_valid_unique_definitions():
    child = {"name": "research", "description": "Find evidence", "system_prompt": "Be careful", "tools": ["read"]}
    assert AgentConfigInput(tools=["call_subagents"], subagents=[child]).subagents[0].name == "research"
    for payload in (
        {"tools": None, "subagents": [child]},
        {"tools": ["call_subagents"], "subagents": []},
        {"tools": ["call_subagents"], "subagents": [child, child]},
        {"tools": ["unknown"], "subagents": []},
        {"tools": ["read", "read"], "subagents": []},
        {"tools": ["call_subagents"], "subagents": [{**child, "tools": ["call_subagents"]}]},
    ):
        with pytest.raises(ValidationError):
            AgentConfigInput.model_validate(payload)


def test_rag_tool_requires_bound_knowledge_base_in_main_or_child():
    child = SubagentDefinitionInput(name="research", description="Find evidence", system_prompt="Find it", tools=["rag_search"])
    with pytest.raises(ValueError, match="rag_tool_requires_knowledge_base"):
        validate_rag_binding(AgentConfigInput(tools=["call_subagents"], subagents=[child]), False)
    validate_rag_binding(AgentConfigInput(tools=["call_subagents"], subagents=[child]), True)
