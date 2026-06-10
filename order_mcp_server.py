import json
import logging
import re

import mysql.connector
import uvicorn
from python_a2a.mcp import FastMCP, create_fastapi_app

from config import Config
from date_encoder import DateEncoder, normalize_mysql_value


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BLOCKED_SQL_WORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|create|replace|grant|revoke|load|outfile|infile)\b",
    re.IGNORECASE,
)


class OrderService:
    def __init__(self):
        self.conf = Config()
        self.conn = mysql.connector.connect(
            host=self.conf.mysql_host,
            port=self.conf.mysql_port,
            user=self.conf.mysql_user,
            password=self.conf.mysql_password,
            database=self.conf.mysql_database,
            autocommit=True,
        )

    def _ensure_connection(self):
        if not self.conn.is_connected():
            self.conn.reconnect(attempts=3, delay=1)

    def _validate_select_sql(self, sql: str) -> str:
        cleaned = " ".join(sql.strip().split())
        if not cleaned:
            raise ValueError("SQL不能为空")
        if ";" in cleaned or "--" in cleaned or "/*" in cleaned or "*/" in cleaned:
            raise ValueError("只允许单条SELECT查询")
        if not cleaned.lower().startswith("select "):
            raise ValueError("订单库工具只允许SELECT查询")
        if BLOCKED_SQL_WORDS.search(cleaned):
            raise ValueError("SQL包含禁止关键字")
        if not re.search(r"\bcustomer_orders\b", cleaned, re.IGNORECASE):
            raise ValueError("只允许查询customer_orders表")
        if not re.search(r"\blimit\b", cleaned, re.IGNORECASE):
            cleaned = f"{cleaned} LIMIT 5"
        return cleaned

    def execute_query(self, sql: str) -> str:
        try:
            safe_sql = self._validate_select_sql(sql)
            logger.info("Executing safe order SQL: %s", safe_sql)
            self._ensure_connection()
            cursor = self.conn.cursor(dictionary=True)
            cursor.execute(safe_sql)
            rows = cursor.fetchall()
            cursor.close()

            normalized_rows = [
                {key: normalize_mysql_value(value) for key, value in row.items()}
                for row in rows
            ]
            payload = (
                {"status": "success", "data": normalized_rows}
                if normalized_rows
                else {"status": "no_data", "message": "暂未查到匹配的订单信息，请核对手机号、下单时间或商品信息。"}
            )
            return json.dumps(payload, cls=DateEncoder, ensure_ascii=False)
        except Exception as exc:
            logger.exception("Order query failed")
            return json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False)


def create_order_mcp_server():
    conf = Config()
    order_mcp = FastMCP(
        name="OrderTools",
        description="只读订单库查询工具，基于order_information.customer_orders表。",
        version="1.0.0",
    )
    service = OrderService()

    @order_mcp.tool(
        name="query_orders",
        description=(
            "执行受限SELECT订单查询。只能查询customer_orders表，用于根据手机号、下单日期、订单ID、"
            "商品特征等信息找出真实物流单号。"
        ),
    )
    def query_orders(sql: str) -> str:
        return service.execute_query(sql)

    logger.info("=== Order MCP Server ===")
    for tool in order_mcp.get_tools():
        logger.info("- %s: %s", tool["name"], tool["description"])

    app = create_fastapi_app(order_mcp)
    logger.info("Order MCP listening on http://localhost:%s", conf.order_mcp_port)
    uvicorn.run(app, host=conf.order_mcp_host, port=conf.order_mcp_port)


if __name__ == "__main__":
    create_order_mcp_server()
