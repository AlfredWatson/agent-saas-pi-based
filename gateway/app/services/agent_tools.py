"""Validated, explicit Pi tool configuration shared by public and internal APIs."""

from pydantic import BaseModel, Field, field_validator, model_validator

BUILTIN_TOOLS = frozenset({"read", "bash", "edit", "write", "grep", "find", "ls"})
MAIN_TOOLS = BUILTIN_TOOLS | {"rag_search", "call_subagents"}
CHILD_TOOLS = MAIN_TOOLS - {"call_subagents"}
DEFAULT_TOOLS = ["read", "bash", "edit", "write"]


def validate_tools(value: list[str], allowed: frozenset[str] | set[str]) -> list[str]:
    if len(value) > len(allowed) or len(set(value)) != len(value) or any(item not in allowed for item in value):
        raise ValueError("invalid_session_tools")
    return value


class SubagentDefinitionInput(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=500)
    system_prompt: str = Field(min_length=1, max_length=20_000)
    tools: list[str]

    @field_validator("name", "description", "system_prompt")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 and char not in "\n\t" for char in value):
            raise ValueError("invalid_subagent_definition")
        return value.strip()

    @field_validator("name")
    @classmethod
    def simple_name(cls, value: str) -> str:
        if any(char.isspace() for char in value):
            raise ValueError("invalid_subagent_name")
        return value

    @field_validator("tools")
    @classmethod
    def child_tools(cls, value: list[str]) -> list[str]:
        return validate_tools(value, CHILD_TOOLS)


class AgentConfigInput(BaseModel):
    tools: list[str] | None = None
    subagents: list[SubagentDefinitionInput] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def valid_configuration(self):
        if self.tools is not None:
            validate_tools(self.tools, MAIN_TOOLS)
        names = [item.name for item in self.subagents]
        if len(set(names)) != len(names):
            raise ValueError("duplicate_subagent_name")
        if self.subagents and (self.tools is None or "call_subagents" not in self.tools):
            raise ValueError("subagents_tool_required")
        if self.tools is not None and "call_subagents" in self.tools and not self.subagents:
            raise ValueError("subagents_required")
        return self


def effective_tools(tools: list[str] | None, has_knowledge_bases: bool) -> list[str]:
    if tools is not None:
        return tools
    return [*DEFAULT_TOOLS, *(["rag_search"] if has_knowledge_bases else [])]


def validate_rag_binding(config: AgentConfigInput, has_knowledge_bases: bool) -> None:
    if not has_knowledge_bases and (
        (config.tools is not None and "rag_search" in config.tools)
        or any("rag_search" in item.tools for item in config.subagents)
    ):
        raise ValueError("rag_tool_requires_knowledge_base")
