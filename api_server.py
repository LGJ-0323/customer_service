import json
import logging
import re
from datetime import datetime, timezone
import uuid

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from python_a2a import A2AClient, Message, MessageRole, Task, TextContent

from config import Config


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

conf = Config()
app = FastAPI(title="Intelligent Customer Service API", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")


class ChatRequest(BaseModel):
    query: str = Field(..., min_length=1)
    phone: str = ""
    conversation_history: str = ""
    session_id: str = ""


def mask_phone_text(text: str) -> str:
    return PHONE_RE.sub(lambda match: f"{match.group(1)[:3]}****{match.group(1)[7:]}", text)


def sanitize_payload(value):
    if isinstance(value, str):
        return mask_phone_text(value)
    if isinstance(value, list):
        return [sanitize_payload(item) for item in value]
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            if key.lower() in {"phone", "mobile", "session_phone"} and isinstance(item, str):
                sanitized[key] = mask_phone_text(item)
            else:
                sanitized[key] = sanitize_payload(item)
        return sanitized
    return value


def verify_api_key(x_api_key: str):
    if conf.api_token and x_api_key != conf.api_token:
        raise HTTPException(status_code=401, detail="Invalid API token")


def provider_label() -> str:
    if conf.logistics_provider == "baidiyun":
        return "百递云实时接口"
    if conf.kdniao_enabled:
        return "快递鸟实时接口"
    return "本地沙盒轨迹"


SESSION_STORE = {}


def get_session(session_id: str) -> tuple[str, dict]:
    sid = session_id.strip() or str(uuid.uuid4())
    session = SESSION_STORE.setdefault(sid, {"history": "", "updated_at": ""})
    return sid, session


def append_session_history(session: dict, query: str, answer: str):
    current = session.get("history", "")
    session["history"] = f"{current}\nUser: {query}\nAssistant: {answer}".strip()
    session["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


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


@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "router_url": f"http://localhost:{conf.router_port}",
        "order_mcp_url": conf.order_mcp_url,
        "logistics_mcp_url": conf.logistics_mcp_url,
        "auth_enabled": bool(conf.api_token),
        "logistics_provider": provider_label(),
    }


@app.post("/api/chat")
async def chat(payload: ChatRequest, x_api_key: str = Header(default="")):
    verify_api_key(x_api_key)
    try:
        session_id, session = get_session(payload.session_id)
        conversation_history = payload.conversation_history.strip() or session.get("history", "")
        client = A2AClient(f"http://localhost:{conf.router_port}")
        router_payload = json.dumps(
            {
                "query": payload.query.strip(),
                "phone": payload.phone.strip(),
                "conversation_history": conversation_history,
                "session_id": session_id,
            },
            ensure_ascii=False,
        )
        message = Message(content=TextContent(text=router_payload), role=MessageRole.USER)
        task = Task(id=str(uuid.uuid4()), message=message.to_dict())
        result = await client.send_task_async(task)
        result_dict = result.to_dict() if hasattr(result, "to_dict") else result
        answer = extract_text(result)
        append_session_history(session, payload.query.strip(), answer)
        return {
            "status": "success",
            "session_id": session_id,
            "answer": mask_phone_text(answer),
            "data_source": provider_label(),
            "conversation_history": mask_phone_text(session["history"]),
            "task": sanitize_payload(result_dict),
        }
    except Exception as exc:
        logger.exception("API chat failed")
        return {
            "status": "error",
            "answer": "服务暂时不可用，请确认 Router、订单 MCP 和物流 MCP 已启动。",
            "data_source": provider_label(),
            "detail": str(exc),
        }


@app.post("/api/session/reset")
def reset_session(x_api_key: str = Header(default="")):
    verify_api_key(x_api_key)
    session_id = str(uuid.uuid4())
    SESSION_STORE[session_id] = {"history": "", "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    return {"status": "success", "session_id": session_id, "conversation_history": ""}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=conf.api_host, port=conf.api_port)
