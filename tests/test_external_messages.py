from inspect_ai.model import ChatMessageAssistant, ChatMessageTool

from evals_app.worker import external_messages


def test_tool_messages_preserve_external_identity_arguments_results_and_errors():
    observation = {
        "facts": {
            "process": {
                "calls": [
                    {
                        "id": "native-span-1",
                        "name": "lookup",
                        "arguments": {"tenant": "a"},
                        "result": {"rows": [1]},
                        "error": "temporary failure",
                    }
                ]
            }
        }
    }
    messages = external_messages(observation)
    assistant, tool = messages
    assert isinstance(assistant, ChatMessageAssistant)
    assert assistant.tool_calls[0].id == "native-span-1"
    assert assistant.tool_calls[0].arguments == {"tenant": "a"}
    assert assistant.source == "input"
    assert isinstance(tool, ChatMessageTool)
    assert tool.tool_call_id == "native-span-1" and tool.function == "lookup"
    assert tool.text == '{"rows": [1]}'
    assert tool.error.message == "temporary failure"
    assert tool.metadata["origin"] == "external_application"


def test_missing_parameters_or_results_are_not_invented():
    observation = {
        "facts": {
            "process": {
                "calls": [
                    "lookup",
                    {"name": "lookup"},
                    {"id": "native-span-2", "name": "lookup", "arguments": {"id": 1}},
                ]
            }
        }
    }
    messages = external_messages(observation)
    assert len(messages) == 2
    assert "not exposed" in messages[-1].text
    assert messages[-1].metadata["result_observed"] is False
    assert external_messages(None) == []
