import asyncio
import json
import logging
import re
from datetime import datetime, timedelta

import pytz
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from python_a2a import A2AServer, AgentCard, AgentSkill, TaskState, TaskStatus, run_server
from python_a2a.mcp import MCPClient

from config import Config


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

ROUTER_SESSION_SLOTS = {}

ORDER_FIELDS = (
    "order_id, order_date, phone, product_name, product_features, quantity, order_amount, "
    "receiver_city, shipping_status, logistics_company, tracking_number, shipped_at, delivered_at"
)

ROUTER_PROMPT = ChatPromptTemplate.from_template(
    """
你是一个电商客服Router Agent，只负责意图识别、槽位抽取和工具分发决策。
你只能输出严格JSON，不能输出Markdown、解释或代码块。

当前日期：{current_date}，时区：Asia/Shanghai。

支持意图：
- query_logistics：用户想查订单发货状态、快递进度、物流轨迹、是否发货、什么时候到。
- general_reply：普通客服咨询，例如问候、发货时效、退换货、发票、转人工、投诉等。
- out_of_scope：明显无关问题。

槽位定义：
- phone：手机号。前端如果传入session_phone，优先使用session_phone。
- order_id：订单号或下单ID。
- tracking_number：物流单号。
- order_name：订单名称或商品名称，例如“青轴键盘”“电竞耳机”。
- order_date：明确单日，YYYY-MM-DD。
- date_range：日期范围，包含start/end，YYYY-MM-DD。
- product_keywords：商品关键词数组，例如["青轴","键盘"]。
- logistics_company：快递公司。

规则：
1. “今天/昨天/前天/明天”等相对日期必须换算为YYYY-MM-DD。
2. 用户直接给了tracking_number时，need_order_lookup=false。
3. 用户直接给了order_id时，可以用order_id查订单；没有tracking_number时，need_order_lookup=true。
4. 用户没有tracking_number且没有order_id时，必须收集订单手机号phone和订单名称order_name。
5. 如果phone缺失，必须status=input_required并追问订单手机号。
6. 如果order_name缺失，必须status=input_required并追问订单名称或商品名称。
7. 如果phone和order_name都缺失，必须一次性追问“订单手机号和订单名称”。
8. 只做识别，不生成SQL，不编造结果。

输出格式固定为：
{{
  "status": "ok | input_required | out_of_scope | error",
  "intent": "query_logistics | general_reply | out_of_scope",
  "need_order_lookup": true,
  "slots": {{
    "phone": "",
    "order_id": "",
    "tracking_number": "",
    "order_name": "",
    "order_date": "",
    "date_range": {{"start": "", "end": ""}},
    "product_keywords": [],
    "logistics_company": ""
  }},
  "follow_up_message": ""
}}

对话历史：
{conversation_history}

前端登录手机号：
{session_phone}

用户最新输入：
{query}
"""
)

SQL_PROMPT = ChatPromptTemplate.from_template(
    """
你是Text-to-SQL生成器，只能基于order_information.customer_orders表生成单条SELECT语句。
输出纯SQL，不要Markdown，不要解释。

安全要求：
- 只能SELECT以下字段：{fields}
- 只能查询customer_orders表。
- 必须带LIMIT，最多LIMIT 5。
- 禁止INSERT、UPDATE、DELETE、DROP、ALTER、CREATE等写操作。
- 有phone时必须加phone条件。
- 有order_id时优先用order_id精确匹配。
- 有order_date时使用DATE(order_date) = 'YYYY-MM-DD'。
- 有date_range时使用DATE(order_date) BETWEEN start AND end。
- 有order_name时必须匹配product_name或product_features，使用LIKE。
- product_keywords要匹配product_name或product_features，使用LIKE。
- 结果按order_date DESC排序。

槽位JSON：
{slots}
"""
)


def current_date_text() -> str:
    return datetime.now(pytz.timezone("Asia/Shanghai")).strftime("%Y-%m-%d")


def extract_text_from_task(task) -> str:
    message_data = task.message or {}
    content = message_data.get("content", {})
    if isinstance(content, dict):
        return content.get("text", "")
    return ""


def parse_router_payload(text: str) -> tuple[str, str, str, str]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text, "", "", ""
    if not isinstance(payload, dict):
        return text, "", "", ""
    return (
        str(payload.get("query", "")),
        str(payload.get("phone", "") or payload.get("session_phone", "")),
        str(payload.get("conversation_history", "")),
        str(payload.get("session_id", "")),
    )


def clean_json_text(text: str) -> str:
    cleaned = re.sub(r"^```json\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    return match.group(0) if match else cleaned


def safe_sql_text(text: str) -> str:
    cleaned = re.sub(r"^```sql\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    cleaned = cleaned.strip().rstrip(";")
    first_select = re.search(r"select\s+", cleaned, flags=re.IGNORECASE)
    if first_select:
        cleaned = cleaned[first_select.start() :]
    return " ".join(cleaned.split())


def empty_slots() -> dict:
    return {
        "phone": "",
        "order_id": "",
        "tracking_number": "",
        "order_name": "",
        "order_date": "",
        "date_range": {"start": "", "end": ""},
        "product_keywords": [],
        "logistics_company": "",
    }


def extract_basic_slots(query: str, session_phone: str = "") -> dict:
    slots = empty_slots()
    slots["phone"] = session_phone or ""

    phone_match = re.search(r"1[3-9]\d{9}", query)
    if phone_match:
        slots["phone"] = phone_match.group(0)

    order_match = re.search(r"OD\d{12,}", query, flags=re.IGNORECASE)
    if order_match:
        slots["order_id"] = order_match.group(0).upper()

    tracking_match = re.search(r"\b(SF|JD|YT|ZT)\d{8,16}\b", query, flags=re.IGNORECASE)
    if tracking_match:
        slots["tracking_number"] = tracking_match.group(0).upper()

    keyword_candidates = (
        "青轴",
        "红轴",
        "键盘",
        "鼠标",
        "耳机",
        "显示器",
        "手表",
        "硬盘",
        "扩展坞",
        "充电器",
        "音箱",
        "手柄",
        "数据线",
        "支架",
    )
    slots["product_keywords"] = [word for word in keyword_candidates if word in query]
    if slots["product_keywords"]:
        slots["order_name"] = "".join(sorted(slots["product_keywords"], key=lambda word: query.find(word)))

    return slots


def merge_slots(*slot_dicts: dict) -> dict:
    merged = empty_slots()
    for slots in slot_dicts:
        if not slots:
            continue
        for key, value in slots.items():
            if key == "date_range":
                current = merged.setdefault("date_range", {"start": "", "end": ""})
                if isinstance(value, dict):
                    current["start"] = value.get("start") or current.get("start", "")
                    current["end"] = value.get("end") or current.get("end", "")
            elif key == "product_keywords":
                keywords = merged.setdefault("product_keywords", [])
                for keyword in value or []:
                    if keyword and keyword not in keywords:
                        keywords.append(keyword)
            elif value:
                merged[key] = value
    return merged


def missing_order_lookup_slots(slots: dict) -> list[str]:
    if slots.get("tracking_number") or slots.get("order_id"):
        return []
    missing = []
    if not slots.get("phone"):
        missing.append("订单手机号")
    if not slots.get("order_name") and not slots.get("product_keywords"):
        missing.append("订单名称")
    return missing


def build_missing_slots_message(missing_slots: list[str]) -> str:
    if set(missing_slots) == {"订单手机号", "订单名称"}:
        return "请提供订单手机号和订单名称（商品名称），我再帮您查询发货状态。"
    return f"请补充{'和'.join(missing_slots)}，我再帮您查询发货状态。"


def build_general_reply(query: str) -> str:
    query = query.strip()
    if any(word in query for word in ("你好", "您好", "在吗", "有人吗", "hello", "hi")):
        return "您好，我是智能客服助手。您可以咨询订单发货、物流进度、退换货、发票或转人工等问题。"
    if any(word in query for word in ("退货", "退款", "退换", "换货", "售后")):
        return "关于退换货，请先确认商品是否影响二次销售，并准备订单手机号、订单名称和问题描述。若商品存在质量问题，我可以协助您整理售后信息并转人工处理。"
    if any(word in query for word in ("发票", "开票", "抬头")):
        return "开具发票通常需要订单信息、发票抬头和税号。您可以提供订单手机号和订单名称，我会协助核对订单后给出开票指引。"
    if any(word in query for word in ("多久发货", "什么时候发货", "发货时间", "几天发货")):
        return "常规现货订单一般会在付款后24到48小时内发出。若您想查询具体订单，请提供订单手机号和订单名称。"
    if any(word in query for word in ("人工", "客服", "转人工")):
        return "可以的。请补充订单手机号、订单名称和具体问题，我会先帮您整理信息，必要时再转人工客服处理。"
    if any(word in query for word in ("投诉", "差评", "不满意")):
        return "抱歉给您带来不好的体验。请提供订单手机号、订单名称和具体问题，我会优先协助核查并给出处理建议。"
    if any(word in query for word in ("谢谢", "感谢")):
        return "不客气，如果后续还需要查询订单、物流或售后问题，可以继续告诉我。"
    return "我可以协助处理订单发货、物流进度、退换货、发票和转人工等客服问题。若要查询具体订单，请提供订单手机号和订单名称。"


def is_general_reply_query(query: str) -> bool:
    general_markers = (
        "你好",
        "您好",
        "在吗",
        "有人吗",
        "多久发货",
        "发货时间",
        "几天发货",
        "退货",
        "退款",
        "退换",
        "换货",
        "售后",
        "发票",
        "开票",
        "抬头",
        "人工",
        "转人工",
        "投诉",
        "差评",
        "不满意",
        "谢谢",
        "感谢",
    )
    return any(marker in query for marker in general_markers)


def heuristic_route(query: str, session_phone: str, conversation_history: str = "") -> dict:
    today = datetime.strptime(current_date_text(), "%Y-%m-%d").date()
    query = query.strip()
    previous_needs_order_slots = "订单手机号" in conversation_history and ("订单名称" in conversation_history or "商品名称" in conversation_history)
    general_delivery_words = ("多久发货", "发货时间", "几天发货")
    if not previous_needs_order_slots and any(word in query for word in general_delivery_words):
        return {
            "status": "ok",
            "intent": "general_reply",
            "need_order_lookup": False,
            "slots": {},
            "follow_up_message": build_general_reply(query),
        }
    logistics_words = ("订单", "物流", "快递", "发货", "运单", "单号", "到哪", "送到", "签收")
    if not previous_needs_order_slots and not any(word in query for word in logistics_words):
        return {
            "status": "ok",
            "intent": "general_reply",
            "need_order_lookup": False,
            "slots": {},
            "follow_up_message": build_general_reply(query),
        }

    slots = {
        "phone": session_phone,
        "order_id": "",
        "tracking_number": "",
        "order_name": "",
        "order_date": "",
        "date_range": {"start": "", "end": ""},
        "product_keywords": [],
        "logistics_company": "",
    }
    phone_match = re.search(r"1[3-9]\d{9}", query)
    if phone_match:
        slots["phone"] = phone_match.group(0)

    order_match = re.search(r"OD\d{12,}", query, flags=re.IGNORECASE)
    if order_match:
        slots["order_id"] = order_match.group(0).upper()

    tracking_match = re.search(r"\b(SF|JD|YT|ZT)\d{8,16}\b", query, flags=re.IGNORECASE)
    if tracking_match:
        slots["tracking_number"] = tracking_match.group(0).upper()

    if "昨天" in query:
        slots["order_date"] = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    elif "前天" in query:
        slots["order_date"] = (today - timedelta(days=2)).strftime("%Y-%m-%d")
    elif "今天" in query:
        slots["order_date"] = today.strftime("%Y-%m-%d")
    else:
        date_match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", query)
        if date_match:
            y, m, d = date_match.groups()
            slots["order_date"] = f"{int(y):04d}-{int(m):02d}-{int(d):02d}"

    keyword_candidates = (
        "青轴",
        "红轴",
        "键盘",
        "鼠标",
        "耳机",
        "显示器",
        "手表",
        "硬盘",
        "扩展坞",
        "充电器",
        "音箱",
        "手柄",
        "数据线",
        "支架",
    )
    slots["product_keywords"] = [word for word in keyword_candidates if word in query]
    if slots["product_keywords"]:
        slots["order_name"] = "".join(
            sorted(slots["product_keywords"], key=lambda word: query.find(word))
        )
    elif previous_needs_order_slots:
        remainder = re.sub(r"1[3-9]\d{9}", " ", query)
        remainder = re.sub(r"(订单|手机号|手机|名称|商品|查询|查一下|帮我|我的|物流|发货|快递)", " ", remainder)
        remainder = re.sub(r"[\s,，。:：;；]+", " ", remainder).strip()
        if remainder:
            slots["order_name"] = remainder

    if any(word in query for word in ("顺丰", "SF")):
        slots["logistics_company"] = "顺丰速运"
    elif any(word in query for word in ("京东", "JD")):
        slots["logistics_company"] = "京东物流"
    elif "圆通" in query:
        slots["logistics_company"] = "圆通速递"
    elif "中通" in query:
        slots["logistics_company"] = "中通快递"

    need_order_lookup = not bool(slots["tracking_number"])
    if need_order_lookup and not slots["order_id"]:
        missing_slots = []
        if not slots["phone"]:
            missing_slots.append("订单手机号")
        if not slots["order_name"]:
            missing_slots.append("订单名称")

        if missing_slots:
            missing_text = "和".join(missing_slots)
            if set(missing_slots) == {"订单手机号", "订单名称"}:
                follow_up_message = "请提供订单手机号和订单名称（商品名称），我再帮您查询发货状态。"
            else:
                follow_up_message = f"请补充{missing_text}，我再帮您查询发货状态。"
            return {
                "status": "input_required",
                "intent": "query_logistics",
                "need_order_lookup": True,
                "slots": slots,
                "follow_up_message": follow_up_message,
            }

    has_order_lookup_slot = any([slots["phone"], slots["order_id"], slots["order_name"], slots["product_keywords"]])
    if need_order_lookup and not has_order_lookup_slot:
        return {
            "status": "input_required",
            "intent": "query_logistics",
            "need_order_lookup": True,
            "slots": slots,
            "follow_up_message": "请提供订单手机号和订单名称（商品名称），我再帮您查询发货状态。",
        }

    return {
        "status": "ok",
        "intent": "query_logistics",
        "need_order_lookup": need_order_lookup,
        "slots": slots,
        "follow_up_message": "",
    }


class IntelligentCustomerServiceRouter(A2AServer):
    def __init__(self):
        self.conf = Config()
        self.llm = None
        if self.conf.api_key:
            self.llm = ChatOpenAI(
                model=self.conf.model_name,
                api_key=self.conf.api_key,
                base_url=self.conf.api_url,
                temperature=0,
            )
        self.router_chain = ROUTER_PROMPT | self.llm if self.llm else None
        self.sql_chain = SQL_PROMPT | self.llm if self.llm else None
        agent_card = AgentCard(
            name="IntelligentCustomerServiceRouter",
            description="电商客服Router Agent，支持订单库Text-to-SQL检索和物流MCP查询。",
            url=f"http://localhost:{self.conf.router_port}",
            version="1.0.0",
            capabilities={"streaming": False, "memory": True},
            skills=[
                AgentSkill(
                    name="query order logistics",
                    description="识别用户物流查询意图，必要时查订单库补全物流单号，再查询物流轨迹。",
                    examples=["我昨天买的那个青轴键盘发货了吗？", "帮我查一下SF123456789到哪了"],
                )
            ],
        )
        super().__init__(agent_card=agent_card)

    def route_query(self, query: str, session_phone: str, conversation_history: str) -> dict:
        if is_general_reply_query(query):
            return {
                "status": "ok",
                "intent": "general_reply",
                "need_order_lookup": False,
                "slots": {},
                "follow_up_message": build_general_reply(query),
            }
        if not self.router_chain:
            return heuristic_route(query, session_phone, conversation_history)
        try:
            output = self.router_chain.invoke(
                {
                    "current_date": current_date_text(),
                    "conversation_history": conversation_history,
                    "session_phone": session_phone,
                    "query": query,
                }
            ).content.strip()
            logger.info("Router LLM output: %s", output)
            return json.loads(clean_json_text(output))
        except Exception:
            logger.exception("Router LLM failed, fallback to heuristic route")
            return heuristic_route(query, session_phone, conversation_history)

    def build_order_sql(self, slots: dict) -> str:
        if self.sql_chain:
            try:
                output = self.sql_chain.invoke(
                    {
                        "fields": ORDER_FIELDS,
                        "slots": json.dumps(slots, ensure_ascii=False),
                    }
                ).content
                sql = safe_sql_text(output)
                if sql:
                    return sql
            except Exception:
                logger.exception("SQL LLM failed, fallback to template SQL")

        conditions = []
        if slots.get("order_id"):
            conditions.append(f"order_id = '{slots['order_id']}'")
        if slots.get("phone"):
            conditions.append(f"phone = '{slots['phone']}'")
        if slots.get("order_name") and not slots.get("product_keywords"):
            safe_order_name = str(slots["order_name"]).replace("'", "''")
            conditions.append(
                f"(product_name LIKE '%{safe_order_name}%' OR product_features LIKE '%{safe_order_name}%')"
            )
        if slots.get("order_date"):
            conditions.append(f"DATE(order_date) = '{slots['order_date']}'")
        date_range = slots.get("date_range") or {}
        if date_range.get("start") and date_range.get("end"):
            conditions.append(f"DATE(order_date) BETWEEN '{date_range['start']}' AND '{date_range['end']}'")
        for keyword in slots.get("product_keywords") or []:
            safe_keyword = str(keyword).replace("'", "''")
            conditions.append(f"(product_name LIKE '%{safe_keyword}%' OR product_features LIKE '%{safe_keyword}%')")
        where_clause = " AND ".join(conditions) if conditions else "1 = 1"
        return f"SELECT {ORDER_FIELDS} FROM customer_orders WHERE {where_clause} ORDER BY order_date DESC LIMIT 5"

    def call_mcp_tool(self, base_url: str, tool_name: str, **kwargs):
        client = MCPClient(base_url)
        loop = asyncio.get_event_loop_policy().new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(client.call_tool(tool_name, **kwargs))
            return json.loads(result) if isinstance(result, str) else result
        finally:
            loop.close()

    def query_order(self, sql: str) -> dict:
        return self.call_mcp_tool(self.conf.order_mcp_url, "query_orders", sql=sql)

    def query_logistics(self, tracking_number: str, logistics_company: str, phone: str = "") -> dict:
        return self.call_mcp_tool(
            self.conf.logistics_mcp_url,
            "query_logistics",
            tracking_number=tracking_number,
            logistics_company=logistics_company or "",
            phone=phone or "",
        )

    def render_order_without_tracking(self, order: dict) -> str:
        product = order.get("product_name", "该商品")
        status = order.get("shipping_status", "暂无状态")
        order_id = order.get("order_id", "")
        if status in ("待发货", "未发货"):
            return f"您的{product}（订单{order_id}）目前还是{status}，暂未生成物流单号。我会建议您稍后再查。"
        if status == "已取消":
            return f"您的{product}（订单{order_id}）已取消，因此没有发货和物流信息。"
        return f"已查到您的{product}（订单{order_id}）状态为{status}，但暂未记录物流单号，请核对订单信息。"

    def render_final_answer(self, order: dict, logistics: dict) -> str:
        if logistics.get("status") == "no_data":
            return (
                f"已查到您的{order.get('product_name', '商品')}订单，但真实物流接口暂未返回轨迹。"
                f"物流单号是{order.get('tracking_number', logistics.get('tracking_number', ''))}，"
                f"接口提示：{logistics.get('message', '暂无轨迹')}。"
            )
        if logistics.get("status") != "success":
            return "抱歉，物流接口暂时不可用。我已查到订单，但现在无法获取最新物流轨迹，请稍后再试。"

        product = order.get("product_name", "该商品")
        order_id = order.get("order_id", "")
        tracking_number = order.get("tracking_number", logistics.get("tracking_number", ""))
        company = order.get("logistics_company") or logistics.get("logistics_company") or "快递公司"
        traces = logistics.get("latest_traces") or []
        latest = traces[0] if traces else {}
        previous = traces[1] if len(traces) > 1 else {}

        if latest:
            answer = (
                f"您的{product}（订单{order_id}）已发货，{company}单号是{tracking_number}。"
                f"最新物流：{latest.get('time', '')}，{latest.get('status', '暂无轨迹描述')}"
            )
            if latest.get("location"):
                answer += f"（{latest['location']}）"
            answer += "。"
            if previous:
                answer += f"上一条状态是：{previous.get('time', '')}，{previous.get('status', '')}。"
            return answer

        return f"您的{product}（订单{order_id}）已发货，{company}单号是{tracking_number}，但暂未获取到物流轨迹。"

    def render_order_without_tracking_v2(self, order: dict) -> str:
        product = order.get("product_name", "商品")
        order_id = order.get("order_id", "")
        status = order.get("shipping_status", "")
        if status in ("待发货", "未发货", ""):
            return f"您购买的{product}正在打包中，很快就会发货。订单号是{order_id}，目前暂未生成物流单号，发出后我可以继续帮您查询物流进度。"
        if status == "已取消":
            return f"您的{product}（订单{order_id}）已取消，因此暂时没有发货和物流信息。"
        return f"已查到您的{product}（订单{order_id}）当前状态为{status}，但暂未记录物流单号，请核对订单信息或稍后再查。"

    def render_final_answer_v2(self, order: dict, logistics: dict) -> str:
        product = order.get("product_name", "商品")
        order_id = order.get("order_id", "")
        order_status = order.get("shipping_status", "")
        tracking_number = order.get("tracking_number", logistics.get("tracking_number", ""))
        company = order.get("logistics_company") or logistics.get("logistics_company") or "快递公司"

        if order_status in ("待发货", "未发货") and not tracking_number:
            return f"您购买的{product}正在打包中，很快就会发货。订单号是{order_id}，暂时还没有物流单号。"

        if logistics.get("status") == "no_data":
            if order_status in ("待发货", "未发货"):
                return f"您购买的{product}正在打包中，很快就会发货。订单号是{order_id}，物流接口暂时还没有轨迹。"
            return (
                f"已查到您的{product}订单，物流单号是{tracking_number}。"
                f"真实物流接口暂未返回轨迹，接口提示：{logistics.get('message', '暂无轨迹')}。"
            )

        if logistics.get("status") != "success":
            return f"已查到您的{product}订单，但物流接口暂时不可用。物流单号是{tracking_number}，请稍后再试。"

        traces = logistics.get("latest_traces") or []
        latest = traces[0] if traces else {}
        previous = traces[1] if len(traces) > 1 else {}

        if not latest:
            return f"您的{product}已发货，{company}单号是{tracking_number}，但暂未获取到最新物流轨迹。"

        latest_text = latest.get("status", "暂无轨迹描述")
        latest_time = latest.get("time", "")
        latest_location = latest.get("location", "")
        location_text = f"（{latest_location}）" if latest_location else ""

        if order_status == "已签收" or "签收" in latest_text:
            answer = f"您的{product}已签收。最新物流显示：{latest_time}，{latest_text}{location_text}。"
        else:
            answer = f"您的{product}正在运输中。最新物流显示：{latest_time}，{latest_text}{location_text}。"

        answer += f"{company}单号是{tracking_number}。"
        if previous:
            answer += f"上一条物流状态为：{previous.get('time', '')}，{previous.get('status', '')}。"
        return answer

    def handle_task(self, task):
        query, session_phone, conversation_history, session_id = parse_router_payload(extract_text_from_task(task))
        logger.info("User query: %s", query)

        try:
            route = self.route_query(query, session_phone, conversation_history)
            remembered_slots = ROUTER_SESSION_SLOTS.get(session_id, {}) if session_id else {}
            if session_id and (route.get("intent") != "general_reply" or remembered_slots):
                current_slots = extract_basic_slots(query, session_phone)
                merged_slots = merge_slots(remembered_slots, route.get("slots") or {}, current_slots)
                route["slots"] = merged_slots

                if remembered_slots and route.get("intent") in ("general_reply", "out_of_scope"):
                    route["intent"] = "query_logistics"
                    route["need_order_lookup"] = True

                if route.get("intent") == "query_logistics" and route.get("need_order_lookup", True):
                    missing_slots = missing_order_lookup_slots(merged_slots)
                    if missing_slots:
                        ROUTER_SESSION_SLOTS[session_id] = merged_slots
                        route["status"] = "input_required"
                        route["follow_up_message"] = build_missing_slots_message(missing_slots)
                    else:
                        route["status"] = "ok"
                        route["follow_up_message"] = ""
            logger.info("Route result: %s", route)

            if route.get("intent") == "general_reply":
                response_text = route.get("follow_up_message") or build_general_reply(query)
            elif route.get("status") == "out_of_scope":
                response_text = route.get("follow_up_message") or "我目前主要处理订单发货和物流查询。"
            elif route.get("status") == "input_required":
                follow_up_text = route.get("follow_up_message", "请补充订单手机号和订单名称。")
                task.artifacts = [{"parts": [{"type": "text", "text": follow_up_text}]}]
                task.status = TaskStatus(
                    state=TaskState.INPUT_REQUIRED,
                    message={"role": "agent", "content": {"text": follow_up_text}},
                )
                return task
            else:
                slots = route.get("slots") or {}
                if route.get("need_order_lookup", True):
                    sql = self.build_order_sql(slots)
                    logger.info("Generated order SQL: %s", sql)
                    order_response = self.query_order(sql)
                    logger.info("Order response: %s", order_response)
                    if order_response.get("status") != "success":
                        response_text = "抱歉，暂未查到该商品的发货信息，请核对手机号、订单号、下单时间或商品信息。"
                    else:
                        if session_id:
                            ROUTER_SESSION_SLOTS.pop(session_id, None)
                        orders = order_response.get("data", [])
                        order = orders[0] if orders else {}
                        if not order.get("tracking_number"):
                            response_text = self.render_order_without_tracking_v2(order)
                        else:
                            logistics_response = self.query_logistics(
                                order.get("tracking_number", ""),
                                order.get("logistics_company", ""),
                                order.get("phone", ""),
                            )
                            response_text = self.render_final_answer_v2(order, logistics_response)
                else:
                    logistics_response = self.query_logistics(
                        slots.get("tracking_number", ""),
                        slots.get("logistics_company", ""),
                        slots.get("phone", ""),
                    )
                    fake_order = {
                        "product_name": "该订单",
                        "order_id": slots.get("order_id", ""),
                        "tracking_number": slots.get("tracking_number", ""),
                        "logistics_company": slots.get("logistics_company", ""),
                    }
                    response_text = self.render_final_answer_v2(fake_order, logistics_response)

            task.artifacts = [{"parts": [{"type": "text", "text": response_text}]}]
            task.status = TaskStatus(state=TaskState.COMPLETED)
            return task
        except Exception:
            logger.exception("Router task failed")
            task.artifacts = [
                {
                    "parts": [
                        {
                            "type": "text",
                            "text": "抱歉，暂时无法查询该商品的发货信息，请稍后再试或核对订单信息。",
                        }
                    ]
                }
            ]
            task.status = TaskStatus(state=TaskState.COMPLETED)
            return task


def main():
    conf = Config()
    server = IntelligentCustomerServiceRouter()
    logger.info("=== Intelligent Customer Service Router ===")
    logger.info("URL: http://localhost:%s", conf.router_port)
    run_server(server, host=conf.router_host, port=conf.router_port)


if __name__ == "__main__":
    main()
