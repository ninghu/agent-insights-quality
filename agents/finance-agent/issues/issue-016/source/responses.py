from __future__ import annotations

from copy import copy

from agent_framework import (
    ChatContext,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    Message,
    MiddlewareTermination,
    ResponseStream,
)

from .retry import tool_result_payload


def current_tool_results(messages, tool_name: str, account_id: str) -> list[dict]:
    start = next(
        (index for index in range(len(messages) - 1, -1, -1)
         if messages[index].role == "user"),
        len(messages),
    )
    current = messages[start:]
    call_ids = {
        content.call_id
        for message in current if message.role == "assistant"
        for content in message.contents
        if content.type == "function_call"
        and content.name == tool_name
        and content.parse_arguments() == {"account_id": account_id}
        and content.call_id
    }
    results = []
    for message in current:
        if message.role != "tool":
            continue
        for content in message.contents:
            if content.type != "function_result" or content.call_id not in call_ids:
                continue
            result = tool_result_payload(content.result)
            if result is not None:
                results.append(result)
    return results


def latest_user_text(messages) -> str:
    return next(
        (message.text for message in reversed(messages) if message.role == "user"),
        "",
    )


async def terminal_answer(context: ChatContext, call_next, answer: str | None) -> None:
    await call_next()
    if answer is None:
        return
    response = context.result
    if context.stream:
        stream = context.result
        response = await stream.get_final_response()
        buffered = tuple(stream.updates)

        async def replay():
            for update in buffered:
                yield update

        context.result = ResponseStream(replay(), finalizer=ChatResponse.from_updates)
    if any(
        content.type == "function_call"
        for message in response.messages for content in message.contents
    ):
        return
    response = copy(response)
    response.messages = [Message("assistant", [answer])]
    if context.stream:
        async def updates():
            contents = [Content.from_text(answer)]
            if response.usage_details is not None:
                contents.append(Content.from_usage(response.usage_details))
            yield ChatResponseUpdate(
                role="assistant", contents=contents,
                response_id=response.response_id, conversation_id=response.conversation_id,
                model=response.model, created_at=response.created_at,
                finish_reason=response.finish_reason,
                continuation_token=response.continuation_token,
                additional_properties=response.additional_properties,
                raw_representation=response.raw_representation,
            )

        context.result = ResponseStream(updates(), finalizer=ChatResponse.from_updates)
    else:
        context.result = response
    raise MiddlewareTermination
