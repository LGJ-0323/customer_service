import argparse
import asyncio
import json
import uuid

from python_a2a import A2AClient, Message, MessageRole, Task, TextContent

from config import Config


def extract_text(task) -> str:
    task_dict = task.to_dict() if hasattr(task, "to_dict") else task
    status_message = ((task_dict.get("status") or {}).get("message") or {}) if isinstance(task_dict, dict) else {}
    content = status_message.get("content") or {}
    if isinstance(content, dict) and content.get("text"):
        return content["text"]

    for artifact in task_dict.get("artifacts") or []:
        for part in artifact.get("parts") or []:
            if part.get("type") == "text" and part.get("text"):
                return part["text"]
    return json.dumps(task_dict, ensure_ascii=False)


async def ask_router(query: str, phone: str) -> str:
    conf = Config()
    client = A2AClient(f"http://localhost:{conf.router_port}")
    payload = json.dumps({"query": query, "phone": phone}, ensure_ascii=False)
    message = Message(content=TextContent(text=payload), role=MessageRole.USER)
    task = Task(id=str(uuid.uuid4()), message=message.to_dict())
    result = await client.send_task_async(task)
    return extract_text(result)


def main():
    parser = argparse.ArgumentParser(description="Intelligent customer service demo client")
    parser.add_argument("query", nargs="?", default="我昨天买的那个青轴键盘发货了吗？")
    parser.add_argument("--phone", default="", help="frontend session phone")
    args = parser.parse_args()

    print(asyncio.run(ask_router(args.query, args.phone)))


if __name__ == "__main__":
    main()
