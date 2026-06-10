# 智能客服物流查询 Agent

基于 FastMCP、A2A、FastAPI、MySQL 和 LLM Router 的私域电商客服 Agent 原型系统，适用于微信微店、品牌自营小程序、官网商城、企微客服等不具备完整平台客服能力的售后场景。

## 功能链路

1. 用户在前端输入问题，例如“我昨天买的那个青轴键盘发货了吗？”或“你们一般多久发货？”
2. Router Agent 做意图识别和槽位抽取，区分具体订单物流查询和普通客服咨询。
3. 缺少物流单号时，Router 先确认订单手机号和订单名称；普通客服问题直接生成标准回复。
4. 订单 MCP 根据受限 SQL 查询 `order_information.customer_orders`。
5. 查询结果通过 `DateEncoder` 处理 `datetime`、`date`、`timedelta`、`Decimal`。
6. Router 拿到物流单号后调用物流 MCP。
7. 物流 MCP 默认接入百递云/快递100实时查询接口，并只返回最新一到两条轨迹。
8. FastAPI 网关向前端返回自然语言结果、数据来源和脱敏后的原始任务。

## 场景定位

本项目不定位为淘宝、京东、拼多多等成熟电商平台的物流展示替代品，而是面向中小商家私域售后客服：

- 微信微店、品牌自营小程序、官网商城等订单查询能力较弱的渠道
- 用户通过自然语言模糊描述订单和售后问题
- 客服需要在订单系统、物流平台和售后规则之间切换
- Agent 负责追问缺失信息、查询订单与物流、生成客服回复话术

## 目录

```text
.
├── api_server.py                 # FastAPI 网关和前端入口
├── wechat_adapter_server.py      # 微信小程序客服本地模拟 Adapter
├── router_agent.py               # Router Agent
├── order_mcp_server.py           # 订单库 MCP
├── logistics_mcp_server.py       # 物流 MCP
├── date_encoder.py               # MySQL 特殊类型 JSON 编码
├── sql/001_create_customer_orders.sql
├── static/                       # 前端测试页
├── tests/                        # unittest 测试
├── Dockerfile
└── docker-compose.yml
```

## 本地启动

建议使用已有 `agent` conda 环境：

```powershell
cd E:/workplace/intelligent_customer_service
$env:PYTHON_EXE="D:/Anaconda3/envs/agent/python.exe"
.\scripts\start_services.ps1
```

默认端口：

- 订单 MCP：`http://localhost:6101`
- 物流 MCP：`http://localhost:6102`
- Router A2A：`http://localhost:6200`
- 前端测试页：`http://localhost:6300`
- 微信小程序客服模拟 Adapter：`http://localhost:6400`

浏览器打开：

```text
http://localhost:6300
```

手动用 PyCharm 启动时，建议顺序：

```text
1. order_mcp_server.py
2. logistics_mcp_server.py
3. router_agent.py
4. api_server.py
5. wechat_adapter_server.py
```

## 微信小程序客服本地模拟

本阶段不直接连接真实微信后台，先用 `wechat_adapter_server.py` 模拟“微信用户发消息”：

```text
微信用户消息 -> wechat_adapter_server.py -> api_server.py -> Router Agent -> MCP -> 回复
```

启动第五个服务后，先检查：

```powershell
Invoke-RestMethod http://localhost:6400/health
```

第一轮模拟用户咨询：

```powershell
$body = @{
  user_id = "wx_user_001"
  content = "我买的青轴键盘发货了吗？"
} | ConvertTo-Json -Compress

Invoke-RestMethod `
  -Uri http://localhost:6400/wechat/mock `
  -Method Post `
  -ContentType "application/json; charset=utf-8" `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
```

预期会追问手机号。

第二轮继续用同一个 `user_id` 补充手机号：

```powershell
$body = @{
  user_id = "wx_user_001"
  content = "13800138000"
} | ConvertTo-Json -Compress

Invoke-RestMethod `
  -Uri http://localhost:6400/wechat/mock `
  -Method Post `
  -ContentType "application/json; charset=utf-8" `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
```

此时 Adapter 会把 `wx_user_001` 映射成稳定会话 ID，从而接上上一轮的商品槽位。

## 数据库初始化

如果使用本机 MySQL：

```powershell
mysql --default-character-set=utf8mb4 -uroot -p123456 order_information --execute="source E:/workplace/intelligent_customer_service/sql/001_create_customer_orders.sql"
```

示例订单中包含：

```text
手机号：13800138000
订单名称：青轴机械键盘
物流单号：SF123456789
```

注意：示例单号是演示单号，真实百递云接口通常会返回“查询无结果”。换成真实单号和匹配手机号后，会展示真实轨迹。

## 环境变量

复制 `.env.example` 为 `.env`，填写真实配置：

```text
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=123456
MYSQL_DATABASE=order_information

LOGISTICS_PROVIDER=baidiyun
BAIDIYUN_ENDPOINT=https://poll.kuaidi100.com/poll/query.do
BAIDIYUN_KEY=...
BAIDIYUN_CUSTOMER=...

# 可选：启用 FastAPI 简单鉴权
API_TOKEN=...

WECHAT_ADAPTER_PORT=6400
WECHAT_MP_TOKEN=...
WECHAT_MP_APP_ID=...
WECHAT_MP_APP_SECRET=...
```

`.env` 已被 `.gitignore` 和 `.dockerignore` 排除，不应提交到仓库。

## Docker Compose

首次使用前准备 `.env`，然后运行：

```powershell
docker compose up --build
```

Compose 会启动：

- MySQL 8.0，并初始化订单表
- Order MCP
- Logistics MCP
- Router Agent
- FastAPI + 前端页面

如果本机 `3306` 已被 MySQL 占用，Compose 默认把容器 MySQL 暴露到宿主机 `3307`。

## 测试

不依赖 `pytest`，使用标准库 `unittest`：

```powershell
D:/Anaconda3/envs/agent/python.exe -m unittest discover -s tests -v
```

当前覆盖：

- `DateEncoder` 序列化
- Router 槽位追问
- 追问后手机号 + 订单名称补槽
- SQL 白名单和写操作拦截
- 手机号脱敏
- 会话历史追加
- 快递公司编码映射

## 安全说明

当前项目已补充基础安全措施：

- `.env` 不提交
- FastAPI 可选 `X-API-Key` 鉴权
- API 返回脱敏后的手机号
- 前端 raw task 展示的是脱敏结果
- 订单 MCP 只允许查询 `customer_orders` 的单条 SELECT

仍不属于生产级系统。生产环境还需要登录态、权限校验、限流、审计日志、参数化 SQL、更严格的会话状态管理和供应商错误码治理。
