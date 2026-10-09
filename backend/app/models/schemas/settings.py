"""
LLM配置相关的Pydantic模型
"""
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field


class ModelCost(BaseModel):
    """模型成本配置"""
    input: float = Field(default=0, description="输入成本（每百万token）")
    output: float = Field(default=0, description="输出成本（每百万token）")
    cacheRead: float = Field(default=0, description="隐式缓存读取成本（每百万token）")
    cacheWrite: float = Field(default=0, description="缓存写入成本（每百万token）")
    explicitCacheInput: float = Field(default=0, description="显式缓存创建输入单价（每百万token）")
    explicitCacheRead: float = Field(default=0, description="显式缓存命中单价（每百万token）")


class ModelCompat(BaseModel):
    """模型兼容性配置"""
    supportsReasoningEffort: bool = Field(default=False, description="是否支持推理强度控制")
    supportsUsageInStreaming: bool = Field(default=False, description="流式响应是否支持usage")
    maxTokensField: str = Field(default="max_tokens", description="最大token字段名")
    nativeWebSearchTool: bool = Field(default=False, description="是否支持原生网络搜索工具")


class TimeRange(BaseModel):
    """时间范围"""
    start: str = Field(description="开始时间，格式 HH:MM")
    end: str = Field(description="结束时间，格式 HH:MM")


class PeakTime(BaseModel):
    """高峰时间配置"""
    weekdays: List[int] = Field(default_factory=list, description="工作日列表，1-7表示周一到周日")
    timeRange: List[TimeRange] = Field(default_factory=list, description="时间段列表")


class LLMModelConfig(BaseModel):
    """LLM模型配置"""
    id: str = Field(description="模型ID")
    name: str = Field(description="模型显示名称")
    reasoning: bool = Field(default=False, description="是否为推理模型")
    autoCreateExplicitCache: bool = Field(default=False, description="是否自动创建显式缓存(explicit prompt cache)")
    input: List[str] = Field(default_factory=lambda: ["text"], description="支持的输入类型")
    billingMode: Optional[List[str]] = Field(default=None, description="模型级计费模式(优先于provider级)")
    cost: ModelCost = Field(default_factory=ModelCost, description="成本配置")
    costInputStep: Optional[Dict[str, ModelCost]] = Field(default=None, description="阶梯计费配置，key如(0, 32k]")
    contextWindow: int = Field(default=128000, description="上下文窗口大小")
    maxTokens: int = Field(default=4096, description="最大输出token数")
    compat: ModelCompat = Field(default_factory=ModelCompat, description="兼容性配置")
    api: str = Field(default="openai-completions", description="API类型")
    other: Optional[Dict[str, Any]] = Field(default=None, description="自定义扩展配置(自由JSON对象)，如自定义API端点等")


class LLMProviderConfig(BaseModel):
    """LLM Provider配置"""
    baseUrl: str = Field(description="API基础URL")
    apiKey: str = Field(default="", description="API密钥")
    billingMode: Optional[List[str]] = Field(default=None, description="Provider级计费模式")
    peakTime: Optional[PeakTime] = Field(default=None, description="高峰时间配置")
    models: List[LLMModelConfig] = Field(default_factory=list, description="模型列表")


class LLMProviderResponse(BaseModel):
    """Provider响应结构（包含名称作为key）"""
    name: str = Field(description="Provider名称（作为key）")
    config: LLMProviderConfig = Field(description="Provider配置")


class LLMProvidersResponse(BaseModel):
    """所有Provider的响应"""
    providers: List[LLMProviderResponse] = Field(default_factory=list)
    total: int = Field(default=0)


class AddProviderRequest(BaseModel):
    """添加Provider请求"""
    name: str = Field(description="Provider名称")
    baseUrl: str = Field(description="API基础URL")
    apiKey: str = Field(default="", description="API密钥")
    billingMode: Optional[List[str]] = Field(default=None, description="计费模式")
    peakTime: Optional[PeakTime] = Field(default=None, description="高峰时间配置")


class UpdateProviderRequest(BaseModel):
    """更新Provider请求"""
    baseUrl: Optional[str] = Field(default=None, description="API基础URL")
    apiKey: Optional[str] = Field(default=None, description="API密钥")
    billingMode: Optional[List[str]] = Field(default=None, description="计费模式")
    peakTime: Optional[PeakTime] = Field(default=None, description="高峰时间配置")


class AddModelRequest(BaseModel):
    """添加Model请求"""
    model: LLMModelConfig = Field(description="模型配置")


class UpdateModelRequest(BaseModel):
    """更新Model请求"""
    model: LLMModelConfig = Field(description="模型配置")


# ===== MCP 配置 =====

class MCPOAuthConfig(BaseModel):
    """MCP 服务器 OAuth 配置"""
    enabled: bool = Field(default=False, description="是否启用 OAuth")
    scopes: List[str] = Field(default_factory=lambda: ["mcp:tools"], description="OAuth 作用域")


class MCPServerConfigRequest(BaseModel):
    """MCP 服务器配置"""
    name: str = Field(description="服务器名称")
    type: str = Field(default="stdio", description="传输类型: stdio/sse/http/ws/sdk")
    enabled: bool = Field(default=True, description="是否启用")
    # stdio
    command: Optional[str] = Field(default=None, description="stdio 命令路径")
    args: List[str] = Field(default_factory=list, description="stdio 参数")
    env: Dict[str, str] = Field(default_factory=dict, description="环境变量")
    cwd: Optional[str] = Field(default=None, description="工作目录")
    # sse / http / ws
    url: Optional[str] = Field(default=None, description="远程服务器 URL")
    headers: Dict[str, str] = Field(default_factory=dict, description="请求头")
    # sdk
    module: Optional[str] = Field(default=None, description="SDK 模块 (pkg.mod:factory)")
    # OAuth
    oauth: Optional[MCPOAuthConfig] = Field(default=None, description="OAuth 配置")
    # 超时覆盖
    call_timeout_seconds: Optional[float] = Field(default=None, description="工具调用超时(秒)")
    connect_timeout_seconds: Optional[float] = Field(default=None, description="连接超时(秒)")
    # 描述
    description: Optional[str] = Field(default=None, description="服务器描述")


class MCPGlobalConfigRequest(BaseModel):
    """MCP 全局配置更新请求"""
    enabled: Optional[bool] = Field(default=None, description="是否启用 MCP")
    call_timeout_seconds: Optional[float] = Field(default=None, description="默认工具调用超时(秒)")
    connect_timeout_seconds: Optional[float] = Field(default=None, description="默认连接超时(秒)")
    max_consecutive_errors: Optional[int] = Field(default=None, description="最大连续错误次数")
    reconnect_delay_seconds: Optional[float] = Field(default=None, description="重连延迟(秒)")
    reconnect_backoff_max_seconds: Optional[float] = Field(default=None, description="重连退避上限(秒)")


class MCPServerResponse(BaseModel):
    """MCP 服务器响应（配置 + 运行时状态）"""
    name: str
    type: str
    enabled: bool
    status: Optional[str] = Field(default=None, description="运行时状态")
    tools_count: int = Field(default=0, description="已发现工具数")
    last_error: Optional[str] = None
    oauth_required: bool = Field(default=False)
    source: str = Field(default="settings")
    description: Optional[str] = None
    # 完整配置字段
    command: Optional[str] = None
    args: List[str] = Field(default_factory=list)
    url: Optional[str] = None
    headers: Dict[str, str] = Field(default_factory=dict)
    module: Optional[str] = None
    oauth: Optional[Dict[str, Any]] = None


class MCPConfigResponse(BaseModel):
    """MCP 完整配置响应"""
    global_config: Dict[str, Any] = Field(description="全局配置")
    servers: List[MCPServerResponse] = Field(default_factory=list)
    total: int = Field(default=0)


# ===== 工具管理配置 =====

class ToolItemResponse(BaseModel):
    """单个工具的展示信息（注册表 + 全局禁用状态）"""
    name: str = Field(description="工具名（注册表名称）")
    description: str = Field(default="", description="工具描述")
    type: str = Field(default="builtin", description="工具类型: builtin/mcp/extension")
    enabled: bool = Field(default=True, description="是否启用（未被全局 tools.disallowed 命中）")
    system_tool: bool = Field(default=False, description="是否为系统内部工具")
    timeout_seconds: int = Field(default=30, description="当前超时（秒）")
    mcp_server: Optional[str] = Field(default=None, description="MCP 工具所属服务器名")


class ToolToggleRequest(BaseModel):
    """启用/禁用单个工具的请求"""
    enabled: bool = Field(description="true=启用, false=禁用")


class ToolTimeoutConfigRequest(BaseModel):
    """工具超时配置更新请求（对应 tools.timeout_seconds）"""
    default: Optional[int] = Field(default=None, description="默认超时（秒）")
    overrides: Optional[Dict[str, int]] = Field(default=None, description="各工具超时覆盖，key=工具名")


class ToolRateLimitRequest(BaseModel):
    """限流配置更新请求（对应 tools.rate_limiting）"""
    enabled: Optional[bool] = Field(default=None, description="是否启用限流")
    requests_per_minute: Optional[int] = Field(default=None, description="每分钟请求数上限")


class SubagentToolsRequest(BaseModel):
    """subagent 分层工具配置更新请求"""
    disallowed_tools_per_level: Optional[Dict[str, List[str]]] = Field(default=None, description="各层级黑名单，key=层级")
    allowed_tools_per_level: Optional[Dict[str, List[str]]] = Field(default=None, description="各层级白名单，key=层级")


class TeamToolsRequest(BaseModel):
    """团队工具配置更新请求（对应 teams.tools）"""
    disallowed: Optional[List[str]] = Field(default=None, description="团队工具黑名单")
    allowed: Optional[List[str]] = Field(default=None, description="团队工具白名单")


class ToolsConfigResponse(BaseModel):
    """工具管理完整配置响应"""
    tools: List[ToolItemResponse] = Field(default_factory=list, description="全部工具列表")
    disallowed: List[str] = Field(default_factory=list, description="全局禁用列表")
    timeout_seconds: Dict[str, Any] = Field(default_factory=dict, description="超时配置")
    rate_limiting: Dict[str, Any] = Field(default_factory=dict, description="限流配置")
    subagent: Dict[str, Any] = Field(default_factory=dict, description="subagent 分层工具配置")
    team: Dict[str, Any] = Field(default_factory=dict, description="团队工具配置")
    total: int = Field(default=0)
    enabled_count: int = Field(default=0)
    disabled_count: int = Field(default=0)


# ===== 技能管理配置 =====

class SkillItemResponse(BaseModel):
    """单个技能的展示信息"""
    name: str = Field(description="技能名（唯一标识）")
    description: str = Field(default="", description="技能描述")
    type: str = Field(default="builtin", description="技能类型: builtin/plugin/workspace")
    enabled: bool = Field(default=True, description="是否启用（未被全局 skills.disallowed 命中）")
    context: str = Field(default="inline", description="上下文模式: inline/fork")
    version: str = Field(default="1.0.0", description="版本")
    gate_reasons: List[str] = Field(default_factory=list, description="门控/禁用原因")


class SkillToggleRequest(BaseModel):
    """启用/禁用单个技能的请求"""
    enabled: bool = Field(description="true=启用, false=禁用")


class AgentSkillsRequest(BaseModel):
    """某个 agent 的技能配置更新请求（agent.entries.{name}.skills）"""
    disallowed: Optional[List[str]] = Field(default=None, description="该 agent 技能黑名单")
    allowed: Optional[List[str]] = Field(default=None, description="该 agent 技能白名单")


class SkillsConfigResponse(BaseModel):
    """技能管理完整配置响应"""
    skills: List[SkillItemResponse] = Field(default_factory=list, description="全部技能列表")
    disallowed: List[str] = Field(default_factory=list, description="全局禁用列表")
    agents: Dict[str, Any] = Field(default_factory=dict, description="各 agent 的技能配置，key=agent名")
    total: int = Field(default=0)
    enabled_count: int = Field(default=0)
    disabled_count: int = Field(default=0)


# ===== 记忆配置 =====

class MemoryLLMConfig(BaseModel):
    """记忆系统 LLM 配置"""
    provider: str = Field(default="aliyun", description="LLM 供应商")
    model: str = Field(default="qwen3.8-max", description="模型名称")


class SessionMemoryConfig(BaseModel):
    """会话记忆配置"""
    enabled: bool = Field(default=True, description="是否启用会话记忆")
    first_update_tokens: int = Field(default=10000, description="首次更新 token 阈值")
    increment_tokens: int = Field(default=5000, description="增量更新 token 阈值")
    tool_calls_threshold: int = Field(default=3, description="工具调用次数阈值")
    natural_pause_turns: int = Field(default=2, description="自然暂停轮次阈值")
    summary_min_tokens: int = Field(default=10000, description="摘要最小 token 数")
    summary_max_tokens: int = Field(default=40000, description="摘要最大 token 数")
    summary_min_messages: int = Field(default=5, description="摘要最小消息数")


class MemoryConfigResponse(BaseModel):
    """记忆配置完整响应"""
    enabled: bool = Field(default=True, description="是否启用记忆系统")
    root: str = Field(default=".happy-claw/memory", description="记忆根目录")
    mode: str = Field(default="standard", description="模式: standard/teammem")
    llm: MemoryLLMConfig = Field(default_factory=MemoryLLMConfig, description="LLM 配置")
    scan_max_files: int = Field(default=200, description="扫描最大文件数")
    select_max: int = Field(default=5, description="注入时最多选择文件数")
    per_file_max_lines: int = Field(default=200, description="单文件最大行数")
    per_file_max_bytes: int = Field(default=4096, description="单文件最大字节数")
    session_inject_budget_bytes: int = Field(default=61440, description="会话注入预算字节数")
    index_max_lines: int = Field(default=200, description="索引最大行数")
    index_max_bytes: int = Field(default=25600, description="索引最大字节数")
    freshness_warn_days: int = Field(default=1, description="新鲜度警告天数")
    extract_min_interval_seconds: int = Field(default=300, description="提取最小间隔秒数")
    extract_max_rounds: int = Field(default=5, description="提取最大轮次")
    extract_min_content_chars: int = Field(default=200, description="提取最小内容字符数")
    extract_min_user_messages: int = Field(default=5, description="提取最小用户消息数")
    consolidate_min_interval_hours: int = Field(default=24, description="整合最小间隔小时数")
    consolidate_min_new_conversations: int = Field(default=5, description="整合最小新会话数")
    consolidate_lock_ttl_seconds: int = Field(default=3600, description="整合锁 TTL 秒数")
    lock_ttl_seconds: int = Field(default=600, description="写入锁 TTL 秒数")
    consolidate_scan_throttle_seconds: int = Field(default=600, description="整合扫描节流秒数")
    prefetch_collect_timeout_seconds: int = Field(default=8, description="预取收集超时秒数")
    session_memory: SessionMemoryConfig = Field(default_factory=SessionMemoryConfig, description="会话记忆配置")


class MemoryConfigRequest(BaseModel):
    """记忆配置更新请求（所有字段可选）"""
    enabled: Optional[bool] = Field(default=None, description="是否启用记忆系统")
    root: Optional[str] = Field(default=None, description="记忆根目录")
    mode: Optional[str] = Field(default=None, description="模式: standard/teammem")
    llm: Optional[MemoryLLMConfig] = Field(default=None, description="LLM 配置")
    scan_max_files: Optional[int] = Field(default=None, description="扫描最大文件数")
    select_max: Optional[int] = Field(default=None, description="注入时最多选择文件数")
    per_file_max_lines: Optional[int] = Field(default=None, description="单文件最大行数")
    per_file_max_bytes: Optional[int] = Field(default=None, description="单文件最大字节数")
    session_inject_budget_bytes: Optional[int] = Field(default=None, description="会话注入预算字节数")
    index_max_lines: Optional[int] = Field(default=None, description="索引最大行数")
    index_max_bytes: Optional[int] = Field(default=None, description="索引最大字节数")
    freshness_warn_days: Optional[int] = Field(default=None, description="新鲜度警告天数")
    extract_min_interval_seconds: Optional[int] = Field(default=None, description="提取最小间隔秒数")
    extract_max_rounds: Optional[int] = Field(default=None, description="提取最大轮次")
    extract_min_content_chars: Optional[int] = Field(default=None, description="提取最小内容字符数")
    extract_min_user_messages: Optional[int] = Field(default=None, description="提取最小用户消息数")
    consolidate_min_interval_hours: Optional[int] = Field(default=None, description="整合最小间隔小时数")
    consolidate_min_new_conversations: Optional[int] = Field(default=None, description="整合最小新会话数")
    consolidate_lock_ttl_seconds: Optional[int] = Field(default=None, description="整合锁 TTL 秒数")
    lock_ttl_seconds: Optional[int] = Field(default=None, description="写入锁 TTL 秒数")
    consolidate_scan_throttle_seconds: Optional[int] = Field(default=None, description="整合扫描节流秒数")
    prefetch_collect_timeout_seconds: Optional[int] = Field(default=None, description="预取收集超时秒数")
    session_memory: Optional[SessionMemoryConfig] = Field(default=None, description="会话记忆配置")
