"""ToolSearch - 工具按需发现与延迟加载机制

当可用工具数量较多时（MCP + extension + 非核心内置），将所有工具 schema 塞入上下文
既昂贵又低效。ToolSearch 将工具分为两类：
- always-load：核心工具，每轮都包含完整 schema
- deferred：仅名称/摘要注入 system prompt，LLM 通过调用 tool_search 按需加载

检索层使用轻量 BM25 实现（无外部依赖），匹配工具名、描述与参数名。
"""
import contextvars
import math
import re
import weakref
from collections import Counter
from typing import Dict, Any, List, Optional, Set, Tuple

import structlog

logger = structlog.get_logger()

# =============================================================================
# ContextVar: 当前执行上下文的 ToolSearchEngine
# 用于全局唯一 handler 分发到正确 agent 的 engine 实例
# =============================================================================

current_search_engine: contextvars.ContextVar[Optional["ToolSearchEngine"]] = contextvars.ContextVar(
    'current_search_engine', default=None
)

# =============================================================================
# ContextVar: 当前执行上下文的会话 ID
# 同一个 Agent 可拥有多个会话，延迟工具的加载状态必须按会话隔离，
# 避免会话 A 通过 tool_search 加载的工具泄露到会话 B。
# =============================================================================

current_conversation_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    'current_conversation_id', default=None
)

# 无会话上下文时使用的默认桶（兼容旧调用与单元测试）
_DEFAULT_CONV_KEY = "__default__"

# =============================================================================
# 全局通知机制：MCP 工具变更后通知所有活跃的 ToolSearchEngine 重建索引
# 使用 WeakSet 防止 Agent 被 GC 后引擎仍被引用导致内存泄漏
# =============================================================================

_active_engines: weakref.WeakSet = weakref.WeakSet()


def notify_tools_changed() -> None:
    """通知所有活跃的 ToolSearchEngine 重建索引（MCP 工具注册/注销后调用）"""
    for engine in list(_active_engines):
        try:
            engine.rebuild_index()
        except Exception as e:
            logger.warning("Failed to rebuild tool search index", error=str(e))


# =============================================================================
# BM25 轻量实现
# =============================================================================

def _tokenize(text: str) -> List[str]:
    """分词：按 _、空格、标点切分，转小写，过滤空串"""
    return [t for t in re.split(r'[_\s\-.,;:!?()\[\]{}|/\\@#$%^&*~`"\']+', text.lower()) if t]


class BM25Index:
    """轻量 BM25 实现，用于工具检索。

    每个文档由多个字段组成，不同字段有不同权重：
    - name: 权重 2.0
    - description: 权重 1.0
    - param_names: 权重 0.5
    """

    K1 = 1.5
    B = 0.75

    def __init__(self, documents: Optional[List[Dict[str, Any]]] = None):
        """
        Args:
            documents: [{name: str, description: str, param_names: List[str]}]
        """
        self._docs: List[Dict[str, Any]] = []
        self._doc_tokens: List[Dict[str, Counter]] = []  # 每篇文档各字段的 token 计数
        self._doc_lengths: List[float] = []  # 加权文档长度
        self._avg_dl: float = 0.0
        self._idf: Dict[str, float] = {}
        self._n: int = 0

        if documents:
            self.build(documents)

    def build(self, documents: List[Dict[str, Any]]) -> None:
        """构建索引"""
        self._docs = documents
        self._n = len(documents)
        if self._n == 0:
            return

        self._doc_tokens = []
        self._doc_lengths = []
        # 用于 IDF 计算的文档频率
        df: Counter = Counter()

        for doc in documents:
            name_tokens = _tokenize(doc.get("name", ""))
            desc_tokens = _tokenize(doc.get("description", ""))
            param_tokens = []
            for p in doc.get("param_names", []):
                param_tokens.extend(_tokenize(p))

            field_counters = {
                "name": Counter(name_tokens),
                "description": Counter(desc_tokens),
                "param_names": Counter(param_tokens),
            }
            self._doc_tokens.append(field_counters)

            # 加权文档长度
            weighted_len = (
                sum(name_tokens.__len__() for _ in [1]) * 2.0
                + len(desc_tokens) * 1.0
                + len(param_tokens) * 0.5
            )
            self._doc_lengths.append(weighted_len)

            # DF 统计（一个 token 在一篇文档中出现只计一次）
            all_tokens = set(name_tokens) | set(desc_tokens) | set(param_tokens)
            for t in all_tokens:
                df[t] += 1

        self._avg_dl = sum(self._doc_lengths) / self._n if self._n > 0 else 1.0

        # 计算 IDF
        self._idf = {}
        for term, freq in df.items():
            self._idf[term] = math.log((self._n - freq + 0.5) / (freq + 0.5) + 1.0)

    def search(self, query: str, top_k: int = 5) -> List[Tuple[str, float]]:
        """搜索索引，返回 [(tool_name, score)]，按分数降序。

        如果 BM25 无正分结果，退回到工具名子串匹配。
        """
        if self._n == 0:
            return []

        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        scores: List[Tuple[str, float]] = []
        field_weights = {"name": 2.0, "description": 1.0, "param_names": 0.5}

        for i, doc in enumerate(self._docs):
            score = 0.0
            dl = self._doc_lengths[i] or 1.0

            for field_name, weight in field_weights.items():
                tf_counter = self._doc_tokens[i][field_name]
                for qt in query_tokens:
                    if qt not in tf_counter:
                        continue
                    tf = tf_counter[qt]
                    idf = self._idf.get(qt, 0.0)
                    # BM25 公式
                    numerator = tf * (self.K1 + 1)
                    denominator = tf + self.K1 * (1 - self.B + self.B * dl / self._avg_dl)
                    score += idf * (numerator / denominator) * weight

            if score > 0:
                scores.append((doc["name"], score))

        # 兜底：若 BM25 无正分结果，退回工具名子串匹配
        if not scores:
            query_lower = query.lower().replace("_", " ").replace("-", " ")
            for doc in self._docs:
                name_lower = doc["name"].lower().replace("_", " ").replace("-", " ")
                desc_lower = doc.get("description", "").lower()
                if query_lower in name_lower or query_lower in desc_lower:
                    scores.append((doc["name"], 0.1))
                else:
                    # 逐词匹配
                    for qt in query_tokens:
                        if qt in name_lower or qt in desc_lower:
                            scores.append((doc["name"], 0.05))
                            break

        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]


# =============================================================================
# ToolSearchEngine
# =============================================================================

class ToolSearchEngine:
    """管理延迟工具目录、搜索和加载状态。

    每个 Agent 实例持有独立的 ToolSearchEngine，维护自己的 loaded 状态。
    """

    def __init__(self, registry, config: Optional[Dict[str, Any]] = None):
        """
        Args:
            registry: ToolRegistry 或 FilteredToolRegistry 实例
            config: tool_search 配置字典，来自 settings.json
        """
        self._registry = registry
        config = config or {}

        # 始终加载的工具名集合
        self._always_load: Set[str] = set(config.get("always_load", [
            "read_file", "write_file", "edit_file",
            "bash_command", "glob", "grep",
            "todo_write", "load_skill", "tool_search",
        ]))

        # 搜索参数
        self._default_limit: int = int(config.get("default_limit", 5))
        self._max_limit: int = int(config.get("max_limit", 10))

        # 运行时状态：已通过搜索加载的 deferred 工具
        # ✅ 按会话隔离：{conversation_id: set(tool_name)}。
        # 同一 Agent 的多个会话各自维护 loaded 状态，互不污染。
        self._loaded_by_conv: Dict[str, Set[str]] = {}

        # BM25 索引与目录
        self._index = BM25Index()
        self._catalog: Dict[str, str] = {}  # {tool_name: short_summary}
        self._deferred_names: Set[str] = set()

        # 构建初始索引
        self.rebuild_index()

        # 注册到全局活跃引擎列表（弱引用，Agent 被 GC 后自动移除）
        _active_engines.add(self)

    # ------------------------------------------------------------------
    # 会话隔离辅助
    # ------------------------------------------------------------------

    def _resolve_conv_key(self, conversation_id: Optional[str]) -> str:
        """解析当前应使用的会话 key。

        优先使用显式传入的 conversation_id，其次读取 ContextVar，
        都没有时回退到默认桶（兼容旧调用与单元测试）。
        """
        if conversation_id:
            return conversation_id
        return current_conversation_id.get() or _DEFAULT_CONV_KEY

    def _get_loaded(self, conversation_id: Optional[str] = None) -> Set[str]:
        """获取指定会话的 loaded 工具集合（不存在则创建空集）"""
        key = self._resolve_conv_key(conversation_id)
        return self._loaded_by_conv.setdefault(key, set())

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def get_tools_for_llm(self, conversation_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """返回当前应传给 LLM 的工具完整定义（always_load + loaded）。

        loaded 按会话隔离：仅包含该会话通过 tool_search 加载的工具。
        仍然经过 registry 的过滤规则（FilteredToolRegistry）。
        """
        loaded = self._get_loaded(conversation_id)
        all_defs = self._registry.get_function_definitions()
        result = []
        for d in all_defs:
            name = d["function"]["name"]
            if name in self._always_load or name in loaded:
                result.append(d)
        return result

    def search(self, query: str, limit: Optional[int] = None, conversation_id: Optional[str] = None) -> Dict[str, Any]:
        """搜索 deferred 工具目录，自动加载匹配工具。

        Args:
            query: 搜索查询
            limit: 返回数量上限
            conversation_id: 会话 ID，加载状态仅对该会话生效（缺省读 ContextVar）

        Returns:
            {matches: [{name, description}], loaded_count: int, total_deferred: int}
        """
        limit = min(limit or self._default_limit, self._max_limit)
        loaded = self._get_loaded(conversation_id)

        # 搜索（排除已加载的）
        results = self._index.search(query, top_k=limit + len(loaded))

        matches = []
        newly_loaded = 0
        for name, score in results:
            if len(matches) >= limit:
                break
            if name in loaded:
                continue
            # 检查 registry 过滤是否允许
            reg = self._registry.get(name)
            if reg is None:
                continue
            # 加载该工具
            loaded.add(name)
            newly_loaded += 1
            matches.append({
                "name": name,
                "description": self._catalog.get(name, reg.description),
            })

        logger.info(
            "ToolSearch executed",
            query=query,
            matches=len(matches),
            newly_loaded=newly_loaded,
            total_loaded=len(loaded),
            conversation_id=self._resolve_conv_key(conversation_id),
        )

        return {
            "matches": matches,
            "loaded_count": newly_loaded,
            "total_deferred": len(self._deferred_names),
        }

    def get_deferred_summary(self, conversation_id: Optional[str] = None) -> str:
        """生成 deferred 工具名摘要文本（注入 system prompt）。

        仅列出该会话尚未加载的 deferred 工具。
        """
        loaded = self._get_loaded(conversation_id)
        unloaded = sorted(self._deferred_names - loaded)
        if not unloaded:
            return ""

        lines = [
            "\n## Deferred Tools (ToolSearch)",
            "The following tools are NOT loaded into context. "
            "Use `tool_search(query)` to find and load them when needed:",
        ]
        for name in unloaded:
            summary = self._catalog.get(name, "")
            if summary:
                lines.append(f"- {name}: {summary}")
            else:
                lines.append(f"- {name}")
        lines.append("")
        return "\n".join(lines)

    def rebuild_index(self) -> None:
        """重建 BM25 索引（MCP 工具动态注册/注销后调用）。

        扫描 registry 中所有活跃工具，将非 always_load 的归为 deferred。
        """
        all_tools = self._registry.list_tools()
        logger.info("ToolSearch rebuilding index", registry=self._registry, tool_count=len(all_tools), tool_names=[tool.name for tool in all_tools])
        documents = []
        self._catalog = {}
        self._deferred_names = set()

        for tool in all_tools:
            name = tool.name
            if name in self._always_load:
                continue
            # 跳过 tool_search 自身
            if name == "tool_search":
                continue

            # 提取参数名
            param_names = []
            params = tool.function_definition.parameters
            if isinstance(params, dict):
                properties = params.get("properties", {})
                if isinstance(properties, dict):
                    param_names = list(properties.keys())

            # 生成摘要（截取描述前 80 字符）
            desc = tool.description or ""
            summary = desc[:80].rstrip() + ("..." if len(desc) > 80 else "")

            self._deferred_names.add(name)
            self._catalog[name] = summary

            documents.append({
                "name": name,
                "description": desc,
                "param_names": param_names,
            })

        self._index.build(documents)

        # 清理各会话 loaded 中已不存在的工具
        for conv_key in list(self._loaded_by_conv.keys()):
            self._loaded_by_conv[conv_key] &= self._deferred_names

        logger.debug(
            "ToolSearch index rebuilt",
            deferred_count=len(self._deferred_names),
            conversations_tracked=len(self._loaded_by_conv),
        )

    @property
    def loaded_tools(self) -> Set[str]:
        """当前上下文（会话）已加载的 deferred 工具名"""
        return self._get_loaded().copy()

    def loaded_tools_for(self, conversation_id: Optional[str]) -> Set[str]:
        """指定会话已加载的 deferred 工具名"""
        return self._get_loaded(conversation_id).copy()

    @property
    def deferred_count(self) -> int:
        """deferred 工具总数"""
        return len(self._deferred_names)

    def dispose(self) -> None:
        """释放引擎（从全局活跃列表中移除）"""
        try:
            _active_engines.discard(self)
        except (ValueError, TypeError):
            pass


# =============================================================================
# tool_search 工具 handler（全局单例，通过 ContextVar 分发到当前 agent 的 engine）
# =============================================================================

_tool_search_registered: bool = False


async def _global_tool_search_handler(query: str, limit: int = 5) -> Dict[str, Any]:
    """Search for available tools by keyword. Returns matching tool names and descriptions.
    Matched tools are automatically loaded into context for subsequent use."""
    engine = current_search_engine.get()
    if engine is None:
        return {"error": "ToolSearch is not available in current context", "matches": [], "loaded_count": 0, "total_deferred": 0}
    return engine.search(query, limit)


def ensure_tool_search_registered() -> None:
    """确保 tool_search 工具只注册一次到全局 registry（幂等）。

    handler 通过 ContextVar 分发到当前执行 agent 的 engine 实例，
    避免多 agent 场景下 handler 被覆盖的问题。
    """
    global _tool_search_registered
    if _tool_search_registered:
        return
    from app.tools.registry import tool_registry, ToolPermissionEnum
    defn = get_tool_search_definition()
    tool_registry.register(
        name=defn["name"],
        description=defn["description"],
        handler=_global_tool_search_handler,
        parameters=defn["parameters"],
        required_role=ToolPermissionEnum.PUBLIC,
        timeout_seconds=5,
        metadata={"system_tool": False},
    )
    _tool_search_registered = True


def create_tool_search_handler(engine: ToolSearchEngine):
    """创建绑定到特定 ToolSearchEngine 实例的 tool_search handler。

    .. deprecated:: 使用 ensure_tool_search_registered() + ContextVar 代替。
       保留此函数仅供测试使用。

    Returns:
        async handler function
    """
    async def tool_search(query: str, limit: int = 5) -> Dict[str, Any]:
        """Search for available tools by keyword. Returns matching tool names and descriptions.
        Matched tools are automatically loaded into context for subsequent use."""
        return engine.search(query, limit)

    return tool_search


def get_tool_search_definition() -> Dict[str, Any]:
    """返回 tool_search 工具的注册参数（供 register 调用）"""
    return {
        "name": "tool_search",
        "description": (
            "Search for available tools that are not currently loaded. "
            "Use this when you need a capability that is not in your current tool set. "
            "Returns matching tools and automatically loads them for subsequent calls."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query describing the tool capability you need",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of tools to return (default 5)",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    }
