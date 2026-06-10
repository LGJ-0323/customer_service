import hashlib
import logging
from datetime import datetime, timezone

import requests
import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from config import Config


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

conf = Config()
app = FastAPI(title="WeChat Mini Program Customer Service Adapter", version="0.1.0")

MOCK_SESSION_HISTORY: dict[str, str] = {}


class MockWechatMessage(BaseModel):
    user_id: str = Field(..., min_length=1, description="模拟微信用户 openid")
    content: str = Field(..., min_length=1, description="用户在微信客服框里发送的文本")


def now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def wechat_session_id(user_id: str) -> str:
    return f"wechat_mp:{user_id.strip()}"


def check_wechat_signature(signature: str, timestamp: str, nonce: str) -> bool:
    if not conf.wechat_mp_token:
        return False
    raw = "".join(sorted([conf.wechat_mp_token, timestamp, nonce]))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest() == signature


def call_customer_service_agent(user_id: str, content: str) -> dict:
    session_id = wechat_session_id(user_id)
    history = MOCK_SESSION_HISTORY.get(session_id, "")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if conf.api_token:
        headers["X-API-Key"] = conf.api_token

    response = requests.post(
        f"http://localhost:{conf.api_port}/api/chat",
        json={
            "query": content.strip(),
            "phone": "",
            "conversation_history": history,
            "session_id": session_id,
        },
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("conversation_history"):
        MOCK_SESSION_HISTORY[session_id] = data["conversation_history"]
    return data


@app.get("/health")
def health():
    return {
        "status": "ok",
        "channel": "wechat_miniprogram_mock",
        "api_url": f"http://localhost:{conf.api_port}/api/chat",
        "wechat_adapter_port": conf.wechat_adapter_port,
        "wechat_token_configured": bool(conf.wechat_mp_token),
    }


@app.get("/wechat/callback", response_class=PlainTextResponse)
def verify_wechat_callback(
    signature: str = Query(default=""),
    timestamp: str = Query(default=""),
    nonce: str = Query(default=""),
    echostr: str = Query(default=""),
):
    """Placeholder for the real WeChat server URL verification step."""
    if not conf.wechat_mp_token:
        return echostr or "wechat adapter alive"
    if check_wechat_signature(signature, timestamp, nonce):
        return echostr
    return "invalid signature"


@app.post("/wechat/mock")
def mock_wechat_message(message: MockWechatMessage):
    """Local mock endpoint: simulate a WeChat user message and return the agent reply."""
    user_id = message.user_id.strip()
    content = message.content.strip()
    logger.info("Mock WeChat message user_id=%s content=%s", user_id, content)
    try:
        data = call_customer_service_agent(user_id, content)
        return {
            "status": "success",
            "channel": "wechat_miniprogram_mock",
            "user_id": user_id,
            "session_id": wechat_session_id(user_id),
            "received_text": content,
            "reply": data.get("answer", "客服暂时没有返回内容。"),
            "conversation_history": data.get("conversation_history", ""),
            "agent_status": data.get("status", ""),
            "timestamp": now_text(),
        }
    except Exception as exc:
        logger.exception("Mock WeChat adapter failed")
        return {
            "status": "error",
            "channel": "wechat_miniprogram_mock",
            "user_id": user_id,
            "session_id": wechat_session_id(user_id),
            "received_text": content,
            "reply": "客服系统暂时繁忙，请稍后再试。",
            "detail": str(exc),
            "timestamp": now_text(),
        }


@app.post("/wechat/mock/reset")
def reset_mock_session(user_id: str = Query(..., min_length=1)):
    session_id = wechat_session_id(user_id)
    MOCK_SESSION_HISTORY.pop(session_id, None)
    return {"status": "success", "session_id": session_id, "message": "mock session reset"}


if __name__ == "__main__":
    logger.info("=== WeChat Mini Program Customer Service Adapter ===")
    logger.info("Mock endpoint: http://localhost:%s/wechat/mock", conf.wechat_adapter_port)
    uvicorn.run(app, host=conf.wechat_adapter_host, port=conf.wechat_adapter_port)
