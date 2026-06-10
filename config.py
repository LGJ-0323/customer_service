import os


def load_env_file(path: str = ".env"):
    """Small .env loader so local credentials do not need to be hard-coded."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


class Config:
    """Runtime configuration loaded from environment variables."""

    def __init__(self):
        load_env_file()

        self.model_name = os.getenv("LLM_MODEL", "deepseek-chat")
        self.api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY", "")
        self.api_url = os.getenv("DEEPSEEK_BASE_URL") or os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com")

        self.mysql_host = os.getenv("MYSQL_HOST", "localhost")
        self.mysql_port = int(os.getenv("MYSQL_PORT", "3306"))
        self.mysql_user = os.getenv("MYSQL_USER", "root")
        self.mysql_password = os.getenv("MYSQL_PASSWORD", "123456")
        self.mysql_database = os.getenv("MYSQL_DATABASE", "order_information")

        self.order_mcp_host = os.getenv("ORDER_MCP_HOST", "0.0.0.0")
        self.order_mcp_port = int(os.getenv("ORDER_MCP_PORT", "6101"))
        self.order_mcp_url = os.getenv("ORDER_MCP_URL", f"http://localhost:{self.order_mcp_port}")

        self.logistics_mcp_host = os.getenv("LOGISTICS_MCP_HOST", "0.0.0.0")
        self.logistics_mcp_port = int(os.getenv("LOGISTICS_MCP_PORT", "6102"))
        self.logistics_mcp_url = os.getenv("LOGISTICS_MCP_URL", f"http://localhost:{self.logistics_mcp_port}")

        self.router_host = os.getenv("ROUTER_HOST", "0.0.0.0")
        self.router_port = int(os.getenv("ROUTER_PORT", "6200"))

        self.api_host = os.getenv("API_HOST", "0.0.0.0")
        self.api_port = int(os.getenv("API_PORT", "6300"))
        self.api_token = os.getenv("API_TOKEN", "")

        self.wechat_adapter_host = os.getenv("WECHAT_ADAPTER_HOST", "0.0.0.0")
        self.wechat_adapter_port = int(os.getenv("WECHAT_ADAPTER_PORT", "6400"))
        self.wechat_mp_token = os.getenv("WECHAT_MP_TOKEN", "")
        self.wechat_mp_app_id = os.getenv("WECHAT_MP_APP_ID", "")
        self.wechat_mp_app_secret = os.getenv("WECHAT_MP_APP_SECRET", "")

        self.kdniao_enabled = os.getenv("KDNIAO_ENABLED", "false").lower() == "true"
        self.kdniao_ebusiness_id = os.getenv("KDNIAO_EBUSINESS_ID", "")
        self.kdniao_api_key = os.getenv("KDNIAO_API_KEY", "")
        self.kdniao_endpoint = os.getenv(
            "KDNIAO_ENDPOINT",
            "https://api.kdniao.com/Ebusiness/EbusinessOrderHandle.aspx",
        )

        self.logistics_provider = os.getenv("LOGISTICS_PROVIDER", "baidiyun").lower()
        self.baidiyun_key = os.getenv("BAIDIYUN_KEY", "")
        self.baidiyun_customer = os.getenv("BAIDIYUN_CUSTOMER", "")
        self.baidiyun_endpoint = os.getenv("BAIDIYUN_ENDPOINT", "https://poll.kuaidi100.com/poll/query.do")
