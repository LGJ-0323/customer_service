import base64
import hashlib
import json
import logging
from datetime import datetime, timedelta
from urllib.parse import quote

import requests
import uvicorn
from python_a2a.mcp import FastMCP, create_fastapi_app

from config import Config


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

SHIPPER_CODES = {
    "顺丰": "shunfeng",
    "顺丰速运": "shunfeng",
    "SF": "shunfeng",
    "京东": "jd",
    "京东物流": "jd",
    "JD": "jd",
    "圆通": "yuantong",
    "圆通速递": "yuantong",
    "YT": "yuantong",
    "中通": "zhongtong",
    "中通快递": "zhongtong",
    "ZT": "zhongtong",
}


def _now_text(offset_hours: int = 0) -> str:
    return (datetime.now() - timedelta(hours=offset_hours)).strftime("%Y-%m-%d %H:%M:%S")


SANDBOX_TRACE_MAP = {
    "SF123456789": [
        {"time": _now_text(1), "status": "快件已发往上海转运中心", "location": "苏州分拨中心"},
        {"time": _now_text(8), "status": "快件已由商家交付顺丰速运", "location": "商家仓库"},
    ],
    "JD9988776655": [
        {"time": _now_text(2), "status": "货物正在发往上海青浦营业部", "location": "华东转运中心"},
        {"time": _now_text(10), "status": "订单已完成出库扫描", "location": "京东昆山仓"},
    ],
}


class LogisticsService:
    def __init__(self):
        self.conf = Config()

    def _resolve_shipper_code(self, tracking_number: str, logistics_company: str) -> str:
        company = (logistics_company or "").strip()
        if company in SHIPPER_CODES:
            return SHIPPER_CODES[company]

        prefix = "".join(ch for ch in tracking_number if ch.isalpha()).upper()
        if prefix.startswith("SF"):
            return "shunfeng"
        if prefix.startswith("JD"):
            return "jd"
        if prefix.startswith("YT"):
            return "yuantong"
        if prefix.startswith("ZT"):
            return "zhongtong"
        return company

    def _data_sign(self, request_data: str) -> str:
        digest = hashlib.md5((request_data + self.conf.kdniao_api_key).encode("utf-8")).digest()
        return quote(base64.b64encode(digest).decode("utf-8"))

    def _baidiyun_sign(self, request_data: str) -> str:
        raw = request_data + self.conf.baidiyun_key + self.conf.baidiyun_customer
        return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()

    def _trim_traces(self, traces, limit: int = 2):
        trimmed = []
        for item in traces[:limit]:
            trimmed.append(
                {
                    "time": item.get("AcceptTime") or item.get("time", ""),
                    "status": item.get("AcceptStation") or item.get("status", ""),
                    "location": item.get("Location") or item.get("location", ""),
                }
            )
        return trimmed

    def _mock_query(self, tracking_number: str, logistics_company: str) -> dict:
        traces = SANDBOX_TRACE_MAP.get(tracking_number)
        if not traces:
            prefix = "".join(ch for ch in tracking_number if ch.isalpha()).upper()[:2] or "SF"
            company = logistics_company or {"SF": "顺丰速运", "JD": "京东物流", "YT": "圆通速递", "ZT": "中通快递"}.get(prefix, "模拟物流")
            traces = [
                {"time": _now_text(3), "status": f"{company}已揽收，正在发往目的地转运中心", "location": "始发仓"},
                {"time": _now_text(16), "status": "商家已打印面单，等待快递揽收", "location": "商家仓库"},
            ]
        return {
            "status": "success",
            "source": "sandbox",
            "tracking_number": tracking_number,
            "logistics_company": logistics_company,
            "latest_traces": self._trim_traces(traces),
        }

    def _trim_baidiyun_traces(self, traces, limit: int = 2):
        trimmed = []
        for item in traces[:limit]:
            trimmed.append(
                {
                    "time": item.get("ftime") or item.get("time", ""),
                    "status": item.get("context") or item.get("status", ""),
                    "location": item.get("areaName") or item.get("areaCenter") or "",
                }
            )
        return trimmed

    def _query_baidiyun(self, tracking_number: str, logistics_company: str, phone: str = "") -> dict:
        if not self.conf.baidiyun_key or not self.conf.baidiyun_customer:
            return {
                "status": "error",
                "source": "baidiyun",
                "message": "百递云接口未配置BAIDIYUN_KEY或BAIDIYUN_CUSTOMER。",
                "tracking_number": tracking_number,
            }

        shipper_code = self._resolve_shipper_code(tracking_number, logistics_company)
        if not shipper_code:
            return {
                "status": "error",
                "source": "baidiyun",
                "message": "缺少快递公司编码，无法调用真实快递查询接口。",
                "tracking_number": tracking_number,
            }

        param = {
            "com": shipper_code,
            "num": tracking_number,
            "phone": phone or "",
            "resultv2": "1",
            "show": "0",
            "order": "desc",
        }
        request_data = json.dumps(param, ensure_ascii=False, separators=(",", ":"))
        data = {
            "customer": self.conf.baidiyun_customer,
            "sign": self._baidiyun_sign(request_data),
            "param": request_data,
        }

        response = requests.post(self.conf.baidiyun_endpoint, data=data, timeout=10)
        response.raise_for_status()
        payload = response.json()
        status_code = str(payload.get("status", ""))
        traces = payload.get("data") or []

        if status_code != "200":
            return {
                "status": "no_data",
                "source": "baidiyun",
                "message": payload.get("message") or "真实物流接口暂未查到轨迹。",
                "api_status": status_code or str(payload.get("returnCode", "")),
                "tracking_number": tracking_number,
                "logistics_company": logistics_company,
                "shipper_code": shipper_code,
            }

        return {
            "status": "success",
            "source": "baidiyun",
            "tracking_number": tracking_number,
            "logistics_company": logistics_company,
            "shipper_code": shipper_code,
            "state": payload.get("state", ""),
            "ischeck": payload.get("ischeck", ""),
            "latest_traces": self._trim_baidiyun_traces(traces),
        }

    def _query_kdniao(self, tracking_number: str, logistics_company: str) -> dict:
        shipper_code = SHIPPER_CODES.get(logistics_company, logistics_company)
        request_data = json.dumps(
            {
                "OrderCode": "",
                "ShipperCode": shipper_code,
                "LogisticCode": tracking_number,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        data = {
            "RequestData": request_data,
            "EBusinessID": self.conf.kdniao_ebusiness_id,
            "RequestType": "1002",
            "DataSign": self._data_sign(request_data),
            "DataType": "2",
        }
        response = requests.post(self.conf.kdniao_endpoint, data=data, timeout=8)
        response.raise_for_status()
        payload = response.json()
        if not payload.get("Success"):
            return {
                "status": "error",
                "message": payload.get("Reason") or "物流接口查询失败",
                "tracking_number": tracking_number,
            }
        traces = list(reversed(payload.get("Traces", [])))
        return {
            "status": "success",
            "source": "kdniao",
            "tracking_number": tracking_number,
            "logistics_company": logistics_company,
            "latest_traces": self._trim_traces(traces),
        }

    def query(self, tracking_number: str, logistics_company: str = "", phone: str = "") -> str:
        try:
            tracking_number = tracking_number.strip()
            logistics_company = logistics_company.strip()
            phone = phone.strip()
            if not tracking_number:
                return json.dumps({"status": "input_required", "message": "缺少物流单号。"}, ensure_ascii=False)

            if self.conf.logistics_provider == "baidiyun":
                result = self._query_baidiyun(tracking_number, logistics_company, phone)
            elif self.conf.kdniao_enabled and self.conf.kdniao_ebusiness_id and self.conf.kdniao_api_key:
                result = self._query_kdniao(tracking_number, logistics_company)
            else:
                result = self._mock_query(tracking_number, logistics_company)

            if result.get("status") == "success":
                result["latest_traces"] = result.get("latest_traces", [])[:2]
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            logger.exception("Logistics query failed")
            return json.dumps(
                {
                    "status": "error",
                    "message": "物流接口暂时不可用，请稍后再试。",
                    "detail": str(exc),
                },
                ensure_ascii=False,
            )


def create_logistics_mcp_server():
    conf = Config()
    logistics_mcp = FastMCP(
        name="LogisticsTools",
        description="只读物流轨迹查询工具，支持快递鸟签名适配和本地沙盒轨迹。",
        version="1.0.0",
    )
    service = LogisticsService()

    @logistics_mcp.tool(
        name="query_logistics",
        description="根据物流单号查询最新一到两条物流轨迹，并剪裁冗长字段。",
    )
    def query_logistics(tracking_number: str, logistics_company: str = "", phone: str = "") -> str:
        return service.query(tracking_number, logistics_company, phone)

    logger.info("=== Logistics MCP Server ===")
    for tool in logistics_mcp.get_tools():
        logger.info("- %s: %s", tool["name"], tool["description"])

    app = create_fastapi_app(logistics_mcp)
    logger.info("Logistics MCP listening on http://localhost:%s", conf.logistics_mcp_port)
    uvicorn.run(app, host=conf.logistics_mcp_host, port=conf.logistics_mcp_port)


if __name__ == "__main__":
    create_logistics_mcp_server()
