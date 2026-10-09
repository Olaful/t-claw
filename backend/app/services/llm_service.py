"""LLM服务 - 统一的LLM调用封装"""
import uuid
from typing import Optional, List, Dict, Any, AsyncIterator, Callable
from dataclasses import dataclass

import structlog
from app.config import settings
from app.utils.redis import redis_client

logger = structlog.get_logger()


@dataclass
class LLMResponse:
    """LLM响应"""
    content: str
    tool_calls: Optional[List[Dict[str, Any]]] = None
    finish_reason: Optional[str] = None
    usage: Optional[Dict[str, int]] = None
    model: Optional[str] = None


@dataclass
class LLMStreamChunk:
    """LLM流式响应块"""
    content: Optional[str] = None
    reasoning_content: Optional[str] = None  # DeepSeek思考模式的推理内容
    tool_calls_delta: Optional[List[Dict[str, Any]]] = None
    finish_reason: Optional[str] = None
    is_complete: bool = False
    usage: Optional[Dict[str, Any]] = None  # ✅ Token用量信息（包含缓存命中、completion_tokens_details）


class LLMService:
    """LLM服务 - 统一封装OpenAI和Anthropic API"""
    
    def __init__(
        self,
        provider: str = "deepseek",
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self.provider = provider
        self.model = model or settings.DEFAULT_LLM_MODEL
        self.api_key = api_key
        self.base_url = base_url
        
        self._openai_client = None
        self._anthropic_client = None
        # ✅ 按供应商缓存客户端（支持运行时切换不同供应商）
        self._clients: Dict[str, Any] = {}
    
    def _resolve_endpoint(self, provider: Optional[str], model_id: Optional[str] = None) -> Dict[str, Any]:
        """解析供应商接入信息（baseUrl/apiKey/api 类型）。

        优先从 settings.json 的 agent.llm.providers.{provider} 读取，
        回退到实例级 api_key/base_url。openai 与 deepseek 接口规范一致，
        均使用 openai-completions(AsyncOpenAI)。
        """
        try:
            from app.core.settings_store import resolve_llm_endpoint
            ep = resolve_llm_endpoint(provider or self.provider, model_id)
        except Exception as e:
            logger.warning("Failed to resolve LLM endpoint from settings", provider=provider, error=str(e))
            ep = {
                "provider": provider or self.provider,
                "model_id": model_id,
                "base_url": None,
                "api_key": None,
                "api": "anthropic-messages" if (provider or self.provider) == "anthropic" else "openai-completions",
            }
        # 实例级配置作为回退
        if not ep.get("api_key"):
            ep["api_key"] = self.api_key
        if not ep.get("base_url"):
            ep["base_url"] = self.base_url
        return ep
    
    def _get_client(self, ep: Dict[str, Any]):
        """根据供应商接入信息获取（并缓存）对应的异步客户端。"""
        cache_key = ep.get("provider") or ep.get("api") or "default"
        cached = self._clients.get(cache_key)
        if cached is not None:
            return cached
        
        api = ep.get("api", "openai-completions")
        base_url = ep.get("base_url")
        api_key = ep.get("api_key")
        
        if api == "anthropic-messages":
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic(
                api_key=api_key or settings.ANTHROPIC_API_KEY,
                base_url=base_url or None,
            )
        else:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(
                api_key=api_key or settings.OPENAI_API_KEY,
                base_url=base_url or settings.OPENAI_BASE_URL,
            )
        
        self._clients[cache_key] = client
        return client
    
    @staticmethod
    def _extract_usage(raw_usage: Any) -> Optional[Dict[str, Any]]:
        """从 usage 对象中提取 token 用量信息。

        兼容两种缓存命中 token 格式：
        - OpenAI: prompt_tokens_details.cached_tokens
        - DeepSeek: prompt_cache_hit_tokens（顶层字段）
        支持显式缓存 token：
        - cache_creation_input_tokens（显式缓存创建，不在 prompt_tokens 中）
        - cache_read_input_tokens（显式缓存命中，与 cached_tokens 互斥）
        同时提取 completion_tokens_details（reasoning_tokens 等推理模型思维链用量）。
        """
        if not raw_usage:
            return None
        usage_data: Dict[str, Any] = {
            "prompt_tokens": getattr(raw_usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(raw_usage, "completion_tokens", 0) or 0,
            "total_tokens": getattr(raw_usage, "total_tokens", 0) or 0,
        }
        # 隐式缓存命中 token：优先 DeepSeek 顶层字段，回退 OpenAI 嵌套字段
        cache_hit_tokens = getattr(raw_usage, "prompt_cache_hit_tokens", None)
        if cache_hit_tokens is None:
            prompt_details = getattr(raw_usage, "prompt_tokens_details", None)
            if prompt_details:
                cache_hit_tokens = getattr(prompt_details, "cached_tokens", None)
        if cache_hit_tokens is not None:
            usage_data["cached_tokens"] = cache_hit_tokens

        # 显式缓存 token（Anthropic 等）
        cache_creation = getattr(raw_usage, "cache_creation_input_tokens", None)
        if cache_creation is None:
            prompt_details = getattr(raw_usage, "prompt_tokens_details", None)
            if prompt_details:
                cache_creation = getattr(prompt_details, "cache_creation_input_tokens", None)
        if cache_creation is not None:
            usage_data["cache_creation_input_tokens"] = cache_creation

        cache_read = getattr(raw_usage, "cache_read_input_tokens", None)
        if cache_read is None:
            prompt_details = getattr(raw_usage, "prompt_tokens_details", None)
            if prompt_details:
                cache_read = getattr(prompt_details, "cache_read_input_tokens", None)
        if cache_read is not None:
            usage_data["cache_read_input_tokens"] = cache_read

        # completion_tokens_details（包含 reasoning_tokens 等）
        completion_details = getattr(raw_usage, "completion_tokens_details", None)
        if completion_details:
            details: Dict[str, Any] = {}
            if getattr(completion_details, "reasoning_tokens", None) is not None:
                details["reasoning_tokens"] = completion_details.reasoning_tokens
            if getattr(completion_details, "accepted_prediction_tokens", None) is not None:
                details["accepted_prediction_tokens"] = completion_details.accepted_prediction_tokens
            if getattr(completion_details, "rejected_prediction_tokens", None) is not None:
                details["rejected_prediction_tokens"] = completion_details.rejected_prediction_tokens
            if details:
                usage_data["completion_tokens_details"] = details
        return usage_data
    
    @staticmethod
    def _parse_step_range(range_str: str) -> tuple:
        """解析阶梯计费区间字符串，如 '(0, 32k]' -> (0, 32000)。
        
        支持后缀: k=1000, M/m=1000000
        支持格式: (min, max] 或 (min, max)
        """
        import re
        range_str = range_str.strip()
        # 匹配格式: (左边界, 右边界] 或 (左边界, 右边界)
        m = re.match(r'[\(\[](\d+[kKmM]?)[,\s]+(\d+[kKmM]?)[\)\]]', range_str)
        if not m:
            return (0, float('inf'))
        
        def _parse_num(s: str) -> int:
            s = s.strip()
            multiplier = 1
            if s.endswith('k') or s.endswith('K'):
                multiplier = 1000
                s = s[:-1]
            elif s.endswith('M') or s.endswith('m'):
                multiplier = 1_000_000
                s = s[:-1]
            return int(float(s) * multiplier)
        
        return (_parse_num(m.group(1)), _parse_num(m.group(2)))
    
    @staticmethod
    def _resolve_cost_config(
        model_cfg: Dict[str, Any],
        provider_cfg: Dict[str, Any],
        prompt_tokens: int,
    ) -> tuple:
        """解析计费模式和对应的成本配置。
        
        Returns:
            (billing_modes: List[str], cost_config: Dict[str, Any])
        """
        # 确定计费模式: 模型级 > provider级 > 默认
        billing_modes = model_cfg.get("billingMode") or provider_cfg.get("billingMode") or []
        
        # 如果是 inputStep 模式，根据 prompt_tokens 匹配阶梯配置
        if "inputStep" in billing_modes:
            cost_input_step = model_cfg.get("costInputStep", {}) or {}
            if cost_input_step:
                for range_str, step_cost in cost_input_step.items():
                    low, high = LLMService._parse_step_range(range_str)
                    if low < prompt_tokens <= high:
                        return (billing_modes, step_cost if hasattr(step_cost, 'get') else {})
                # 没有匹配的区间，回退到默认 cost
                logger.warning(f"No matching inputStep range for {prompt_tokens} tokens")
        
        # 默认使用模型的 cost 配置
        return (billing_modes, model_cfg.get("cost", {}) or {})

    @staticmethod
    def _calculate_cost(
        usage: Dict[str, Any],
        provider: str,
        model_id: str,
    ) -> Dict[str, Any]:
        """计算 Token 费用。
        
        支持多种计费模式：
        - peakTime: 高峰/空闲分段计费，非高峰时段价格减半
        - inputStep: 按输入token量分阶梯计费
        
        支持显式缓存token计费：
        - cache_creation_input_tokens: 显式缓存创建输入token
        - cache_read_input_tokens: 显式缓存命中 token
        
        Args:
            usage: Token 用量信息
            provider: 供应商名称
            model_id: 模型 ID
        
        Returns:
            费用信息字典，包含总费用和明细
        """
        try:
            from app.config import settings
            data = settings.to_dict()
            providers = (data.get("agent", {}) or {}).get("llm", {}).get("providers", {}) or {}
            provider_cfg = providers.get(provider, {}) or {}
            
            # 查找模型配置
            model_cfg = None
            for m in provider_cfg.get("models", []) or []:
                if hasattr(m, 'get') and m.get("id") == model_id:
                    model_cfg = m
                    break
            
            if not model_cfg:
                logger.warning(f"No model config found for {provider}/{model_id}")
                return {"total_cost": 0.0, "breakdown": {}}
            
            # 提取 token 用量
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            cached_tokens = usage.get("cached_tokens", 0)  # 隐式缓存命中
            cache_creation_tokens = usage.get("cache_creation_input_tokens", 0)  # 显式缓存创建
            cache_read_tokens = usage.get("cache_read_input_tokens", 0)  # 显式缓存命中
            
            # 解析计费模式和对应的成本配置
            billing_modes, cost_config = LLMService._resolve_cost_config(
                model_cfg, provider_cfg, prompt_tokens
            )
            
            if not cost_config:
                logger.warning(f"No cost config found for {provider}/{model_id}")
                return {"total_cost": 0.0, "breakdown": {}}
            
            # 提取单价（每百万 token 的价格）
            input_price = cost_config.get("input", 0) / 1_000_000
            output_price = cost_config.get("output", 0) / 1_000_000
            cache_read_price = cost_config.get("cacheRead", 0) / 1_000_000
            cache_write_price = cost_config.get("cacheWrite", 0) / 1_000_000
            explicit_cache_input_price = cost_config.get("explicitCacheInput", 0) / 1_000_000
            explicit_cache_read_price = cost_config.get("explicitCacheRead", 0) / 1_000_000
            
            # 计算非缓存的输入 token
            # 注意: cached_tokens 与 cache_read_input_tokens 互斥
            non_cached_input = max(0, prompt_tokens - cached_tokens - cache_read_tokens)
            
            # 基础费用计算
            input_cost = non_cached_input * input_price
            output_cost = completion_tokens * output_price
            # 隐式缓存读取费用
            implicit_cache_read_cost = cached_tokens * cache_read_price
            # 显式缓存费用
            explicit_cache_input_cost = cache_creation_tokens * explicit_cache_input_price
            explicit_cache_read_cost = cache_read_tokens * explicit_cache_read_price
            cache_write_cost = 0  # 待具体provider支持后扩展
            
            total_cost = (
                input_cost + output_cost + implicit_cache_read_cost +
                explicit_cache_input_cost + explicit_cache_read_cost + cache_write_cost
            )
            
            # peakTime 模式: 非高峰时段价格减半
            is_peak = True
            if "peakTime" in billing_modes:
                peak_time = provider_cfg.get("peakTime", {})
                is_peak = LLMService._is_peak_time(peak_time)
                if not is_peak:
                    total_cost *= 0.5
            
            discount = 0.5 if ("peakTime" in billing_modes and not is_peak) else 1.0
            
            breakdown = {
                "input_cost": round(input_cost * discount, 6),
                "output_cost": round(output_cost * discount, 6),
                "implicit_cache_read_cost": round(implicit_cache_read_cost * discount, 6),
                "explicit_cache_input_cost": round(explicit_cache_input_cost * discount, 6),
                "explicit_cache_read_cost": round(explicit_cache_read_cost * discount, 6),
                "cache_write_cost": round(cache_write_cost * discount, 6),
            }
            # 移除值为0的明细项
            breakdown = {k: v for k, v in breakdown.items() if v > 0}
            
            token_usage = {
                "input_tokens": prompt_tokens,
                "output_tokens": completion_tokens,
                "non_cached_input": non_cached_input,
            }
            if cached_tokens:
                token_usage["cached_tokens"] = cached_tokens
            if cache_creation_tokens:
                token_usage["cache_creation_input_tokens"] = cache_creation_tokens
            if cache_read_tokens:
                token_usage["cache_read_input_tokens"] = cache_read_tokens
            
            result = {
                "total_cost": round(total_cost, 6),
                "billing_mode": billing_modes,
                "breakdown": breakdown,
                "token_usage": token_usage,
            }
            if "peakTime" in billing_modes:
                result["is_peak_time"] = is_peak
            if "inputStep" in billing_modes:
                result["input_step_matched"] = True
            
            return result
        except Exception as e:
            logger.error("Failed to calculate cost", error=str(e))
            return {"total_cost": 0.0, "breakdown": {}, "error": str(e)}
    
    @staticmethod
    def _is_peak_time(peak_config: Dict[str, Any]) -> bool:
        """检查当前时间是否在高峰时段。
        
        Args:
            peak_config: 高峰时段配置，格式：
                {
                    "weekdays": [1, 2, 3, 4, 5],  # 1=周一, ..., 7=周日
                    "timeRange": [
                        {"start": "09:00", "end": "12:00"},
                        {"start": "14:00", "end": "18:00"}
                    ]
                }
        
        Returns:
            是否在高峰时段
        """
        if not peak_config:
            return True
        
        from datetime import datetime
        now = datetime.now()
        current_weekday = now.isoweekday()  # 1-7
        current_time = now.strftime("%H:%M")
        
        # 检查是否是工作日
        weekdays = peak_config.get("weekdays", [])
        if current_weekday not in weekdays:
            return False
        
        # 检查时间范围
        time_ranges = peak_config.get("timeRange", [])
        for time_range in time_ranges:
            start = time_range.get("start", "00:00")
            end = time_range.get("end", "23:59")
            if start <= current_time < end:
                return True
        
        return False
    
    def _get_openai_client(self):
        """获取OpenAI客户端（默认供应商）"""
        if self._openai_client is None:
            ep = self._resolve_endpoint(self.provider)
            from openai import AsyncOpenAI
            self._openai_client = AsyncOpenAI(
                api_key=ep.get("api_key") or settings.OPENAI_API_KEY,
                base_url=ep.get("base_url") or settings.OPENAI_BASE_URL,
            )
        return self._openai_client
    
    def _get_anthropic_client(self):
        """获取Anthropic客户端（默认供应商）"""
        if self._anthropic_client is None:
            ep = self._resolve_endpoint(self.provider)
            from anthropic import AsyncAnthropic
            self._anthropic_client = AsyncAnthropic(
                api_key=ep.get("api_key") or settings.ANTHROPIC_API_KEY,
                base_url=ep.get("base_url") or None,
            )
        return self._anthropic_client
    
    async def chat(
        self,
        messages: List[Dict[str, Any]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
    ) -> str:
        """简单聊天接口"""
        response = await self.chat_with_tools(
            messages=messages,
            tools=None,
            temperature=temperature,
            max_tokens=max_tokens,
            model=model,
        )
        return response.content
    
    async def chat_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        service_tier: Optional[str] = None,
    ) -> LLMResponse:
        """带工具调用的聊天接口"""
        ep = self._resolve_endpoint(provider or self.provider, model)
        if ep.get("api") == "anthropic-messages":
            return await self._chat_anthropic(messages, tools, temperature, max_tokens, model, ep)
        return await self._chat_openai(messages, tools, temperature, max_tokens, model, ep, reasoning_effort, service_tier)
    
    async def _chat_openai(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        temperature: Optional[float],
        max_tokens: Optional[int],
        model: Optional[str],
        ep: Optional[Dict[str, Any]] = None,
        reasoning_effort: Optional[str] = None,
        service_tier: Optional[str] = None,
    ) -> LLMResponse:
        """OpenAI API调用"""
        ep = ep or self._resolve_endpoint(self.provider, model)
        client = self._get_client(ep)
        
        kwargs = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature or settings.DEFAULT_TEMPERATURE,
            "max_tokens": max_tokens or settings.DEFAULT_MAX_TOKENS,
        }
        
        # ✅ reasoning_effort 仅在供应商支持时传递
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        
        # ✅ service_tier: OpenAI 服务层级（fast/auto/default/flex/scale/priority）
        if service_tier:
            kwargs["service_tier"] = service_tier
        
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        
        try:
            response = await client.chat.completions.create(**kwargs)
            
            choice = response.choices[0]
            message = choice.message
            
            tool_calls = None
            if message.tool_calls:
                tool_calls = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in message.tool_calls
                ]
            
            return LLMResponse(
                content=message.content or "",
                tool_calls=tool_calls,
                finish_reason=choice.finish_reason,
                usage={
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                } if response.usage else None,
                model=response.model,
            )
        
        except Exception as e:
            logger.error("OpenAI API call failed", error=str(e), exc_info=True)
            raise
    
    async def _chat_anthropic(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        temperature: Optional[float],
        max_tokens: Optional[int],
        model: Optional[str],
        ep: Optional[Dict[str, Any]] = None,
    ) -> LLMResponse:
        """Anthropic API调用"""
        ep = ep or self._resolve_endpoint(self.provider, model)
        client = self._get_client(ep)
        
        # 转换消息格式
        system_msg = ""
        chat_messages = []
        
        for msg in messages:
            if msg["role"] == "system":
                system_msg = msg["content"]
            else:
                chat_messages.append({
                    "role": msg["role"],
                    "content": msg["content"],
                })
        
        kwargs = {
            "model": model or self.model,
            "messages": chat_messages,
            "max_tokens": max_tokens or settings.DEFAULT_MAX_TOKENS,
            "temperature": temperature or settings.DEFAULT_TEMPERATURE,
        }
        
        if system_msg:
            kwargs["system"] = system_msg
        
        if tools:
            # 转换工具格式
            anthropic_tools = []
            for tool in tools:
                func = tool.get("function", {})
                anthropic_tools.append({
                    "name": func.get("name", ""),
                    "description": func.get("description", ""),
                    "input_schema": func.get("parameters", {}),
                })
            kwargs["tools"] = anthropic_tools
        
        try:
            response = await client.messages.create(**kwargs)
            
            content = ""
            tool_calls = []
            
            for block in response.content:
                if block.type == "text":
                    content += block.text
                elif block.type == "tool_use":
                    tool_calls.append({
                        "id": block.id,
                        "type": "function",
                        "function": {
                            "name": block.name,
                            "arguments": str(block.input),
                        },
                    })
            
            return LLMResponse(
                content=content,
                tool_calls=tool_calls if tool_calls else None,
                finish_reason=response.stop_reason,
                usage={
                    "prompt_tokens": response.usage.input_tokens,
                    "completion_tokens": response.usage.output_tokens,
                    "total_tokens": response.usage.input_tokens + response.usage.output_tokens,
                },
                model=response.model,
            )
        
        except Exception as e:
            logger.error("Anthropic API call failed", error=str(e), exc_info=True)
            raise
    
    async def chat_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        service_tier: Optional[str] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,  # ✅ 取消检查回调
        prompt_cache_key: Optional[str] = None,  # ✅ 提示缓存键（供应商需支持 supportsPromptCacheKey）
        conversation_id: Optional[str] = None,  # ✅ 会话ID（用于 X-Session-ID 等会话级标识）
    ) -> AsyncIterator[LLMStreamChunk]:
        """流式聊天"""
        ep = self._resolve_endpoint(provider or self.provider, model)
        if ep.get("api") == "anthropic-messages":
            # 非流式降级
            response = await self._chat_anthropic(messages, tools, temperature, max_tokens, model, ep)
            yield LLMStreamChunk(
                content=response.content,
                tool_calls_delta=response.tool_calls,
                finish_reason=response.finish_reason,
                is_complete=True,
            )
        else:
            async for chunk in self._stream_openai(messages, tools, temperature, max_tokens, model, ep, reasoning_effort, service_tier, is_cancelled, prompt_cache_key, conversation_id):
                yield chunk
    
    async def _stream_openai(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        temperature: Optional[float],
        max_tokens: Optional[int],
        model: Optional[str],
        ep: Optional[Dict[str, Any]] = None,
        reasoning_effort: Optional[str] = None,
        service_tier: Optional[str] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,  # ✅ 取消检查回调
        prompt_cache_key: Optional[str] = None,  # ✅ 提示缓存键
        conversation_id: Optional[str] = None,  # ✅ 会话ID
    ) -> AsyncIterator[LLMStreamChunk]:
        """OpenAI流式调用"""
        ep = ep or self._resolve_endpoint(self.provider, model)
        client = self._get_client(ep)
        
        kwargs = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature or settings.DEFAULT_TEMPERATURE,
            "max_tokens": max_tokens or settings.DEFAULT_MAX_TOKENS,
            "stream": True,
        }
        
        # ✅ reasoning_effort 仅在供应商支持时传递
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        
        # ✅ service_tier: OpenAI 服务层级（fast/auto/default/flex/scale/priority）
        if service_tier:
            kwargs["service_tier"] = service_tier
        
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        
        # ✅ 请求在流式响应中返回 usage（供应商需在 compat.supportsUsageInStreaming 声明支持）
        compat_cfg = (ep.get("params") or {}).get("compat") or {}
        if compat_cfg.get("supportsUsageInStreaming", False):
            kwargs["stream_options"] = {"include_usage": True}
        
        # ✅ prompt_cache_key: 供应商声明 supportsPromptCacheKey 时传递稳定缓存键（如会话ID）
        if prompt_cache_key and compat_cfg.get("supportsPromptCacheKey", False):
            kwargs["prompt_cache_key"] = prompt_cache_key
        
        # ✅ X-Session-ID: 供应商声明 supportsXsessionIdHeader 时通过 extra_headers 传递
        if conversation_id and compat_cfg.get("supportsXsessionIdHeader", False):
            redis_key = f"x-session-id:{conversation_id}"
            idle_timeout = (ep.get("params") or {}).get("XSessionIdIdleTimeout", 3600)
            try:
                session_id = await redis_client.get(redis_key)
                if not session_id:
                    session_id = str(uuid.uuid4())
                    await redis_client.set(redis_key, session_id, ex=idle_timeout)
                    logger.info("New X-Session-ID created", session_id=session_id)
                kwargs["extra_headers"] = {"X-Session-ID": session_id}
            except Exception as e:
                logger.warning("Failed to get/set X-Session-ID from Redis", error=str(e))
        
        # ✅ 显式缓存：模型配置 autoCreateExplicitCache 时开启 explicit 模式
        if (ep.get("params") or {}).get("autoCreateExplicitCache", False):
            kwargs["prompt_cache_options"] = {"mode": "explicit"}
        
        stream = await client.chat.completions.create(**kwargs)
        
        # 用于累积工具调用的各个片段（OpenAI流式工具调用会分多个chunk发送）
        tool_call_accumulator: Dict[int, Dict[str, Any]] = {}
        
        async for chunk in stream:
            # ✅ 检查是否已取消，及时中断流式接收
            if is_cancelled and is_cancelled():
                logger.info("LLM stream cancelled by client")
                break
            
            if not chunk.choices:
                # ✅ include_usage 模式下，最后一个 chunk 的 choices 为空，仅携带 usage
                raw_usage = getattr(chunk, "usage", None)
                if raw_usage:
                    yield LLMStreamChunk(usage=self._extract_usage(raw_usage))
                continue
            
            delta = chunk.choices[0].delta
            
            # 解析工具调用增量
            tool_calls_delta = None
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    
                    if idx not in tool_call_accumulator:
                        tool_call_accumulator[idx] = {
                            "id": "",
                            "type": "function",
                            "function": {
                                "name": "",
                                "arguments": "",
                            },
                        }
                    
                    acc = tool_call_accumulator[idx]
                    
                    if tc_delta.id:
                        acc["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            acc["function"]["name"] += tc_delta.function.name
                        if tc_delta.function.arguments:
                            acc["function"]["arguments"] += tc_delta.function.arguments
                
                # ✅ 输出累加器中全部工具调用（按 index 排序），
                # 而非仅当前 chunk 涉及的 index。OpenAI 流式多工具调用时
                # 每个 chunk 的 delta.tool_calls 通常只携带一个 index，
                # 若只返回当前 chunk 的部分，上层直接替换会丢失其它工具调用，
                # 导致最终只剩最后一个工具调用。
                tool_calls_delta = [
                    tool_call_accumulator[i].copy()
                    for i in sorted(tool_call_accumulator.keys())
                ]
            
            # 解析reasoning_content（DeepSeek思考模式）
            reasoning_content = getattr(delta, 'reasoning_content', None)
            
            # ✅ 收集 usage 信息（部分供应商在 finish_reason chunk 携带 usage）
            usage = self._extract_usage(getattr(chunk, "usage", None))
            
            yield LLMStreamChunk(
                content=delta.content,
                reasoning_content=reasoning_content,
                tool_calls_delta=tool_calls_delta,
                finish_reason=chunk.choices[0].finish_reason,
                is_complete=chunk.choices[0].finish_reason is not None,
                usage=usage,  # ✅ 仅在最后一个 chunk 时返回 usage
            )
    
    async def generate_embedding(self, text: str) -> List[float]:
        """生成文本嵌入向量"""
        if self.provider == "openai":
            client = self._get_openai_client()
            response = await client.embeddings.create(
                input=text,
                model="text-embedding-3-small",
            )
            return response.data[0].embedding
        else:
            raise ValueError(f"Embedding not supported for provider: {self.provider}")
    
    async def count_tokens(self, text: str, model: Optional[str] = None) -> int:
        """计算token数量"""
        try:
            import tiktoken
            encoding = tiktoken.encoding_for_model(model or self.model)
            return len(encoding.encode(text))
        except Exception:
            # 粗略估算
            return len(text) // 4


# 便捷函数
def create_llm_service(
    provider: str = "openai",
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
) -> LLMService:
    """创建LLM服务实例"""
    return LLMService(provider=provider, model=model, api_key=api_key, base_url=base_url)
