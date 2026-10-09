"""内置工具 - Web Search、Bash Command 等常用工具"""
import asyncio
import subprocess
import platform
import os
import json
import re
import uuid
import glob as glob_module
import fnmatch
from pathlib import Path
from typing import Dict, Any, Optional, List
from html import unescape
import structlog
import httpx

from app.tools.registry import tool_registry, ToolPermissionEnum, current_tool_call_id
from app.core.workspace_isolation import resolve_ws_path, get_current_workspace

logger = structlog.get_logger()


def _tool_cwd() -> str:
    """工具执行目录：优先当前agent工作区，无上下文时回退进程当前目录"""
    return get_current_workspace() or os.getcwd()


# ===== Web Search 工具 =====

def _load_web_search_config() -> Dict[str, Any]:
    """从统一 settings 加载 web_search 配置"""
    try:
        from app.config import settings
        ws_config = settings.tools.web_search
        # 返回支持 .get() 的 DotDict 或 dict
        if hasattr(ws_config, 'to_dict'):
            return ws_config.to_dict()
        return ws_config if isinstance(ws_config, dict) else {}
    except Exception as e:
        logger.warning("Failed to load web_search config from settings", error=str(e), exc_info=True)
    return {}


def _get_env(key: str, default: str = "") -> str:
    """从环境变量或 .env 文件获取值"""
    val = os.environ.get(key, "")
    if not val:
        # 尝试从 backend/.env 读取
        env_file = Path(__file__).resolve().parent.parent.parent / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith(f"{key}="):
                    val = line[len(key) + 1:].strip().strip('"').strip("'")
                    break
    return val or default


async def _search_brave(query: str, num_results: int, provider_cfg: dict) -> Dict[str, Any]:
    """Brave Search API（免费层 2000 次/月）"""
    api_key_env = provider_cfg.get("api_key_env", "BRAVE_API_KEY")
    api_key = _get_env(api_key_env)
    if not api_key:
        return {
            "success": False,
            "error": f"Brave API key not configured. Set {api_key_env} in environment or .env file. "
                     "Get a free key at https://brave.com/search/api/",
        }

    base_url = provider_cfg.get("base_url", "https://api.search.brave.com/res/v1/web/search")
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }
    params = {"q": query, "count": min(num_results, 20)}

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(base_url, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()

    results = []
    for item in data.get("web", {}).get("results", [])[:num_results]:
        results.append({
            "title": item.get("title", ""),
            "link": item.get("url", ""),
            "snippet": item.get("description", ""),
        })

    return {"success": True, "query": query, "provider": "brave", "results_count": len(results), "results": results}


async def _search_serpapi(query: str, num_results: int, provider_cfg: dict) -> Dict[str, Any]:
    """SerpAPI - Google 搜索"""
    api_key_env = provider_cfg.get("api_key_env", "SERPAPI_KEY")
    api_key = _get_env(api_key_env)
    if not api_key:
        return {
            "success": False,
            "error": f"SerpAPI key not configured. Set {api_key_env} in environment or .env file. "
                     "Get a key at https://serpapi.com/",
        }

    base_url = provider_cfg.get("base_url", "https://serpapi.com/search")
    params = {"q": query, "num": num_results, "api_key": api_key, "engine": "google"}

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(base_url, params=params)
        resp.raise_for_status()
        data = resp.json()

    results = []
    for item in data.get("organic_results", [])[:num_results]:
        results.append({
            "title": item.get("title", ""),
            "link": item.get("link", ""),
            "snippet": item.get("snippet", ""),
        })

    return {"success": True, "query": query, "provider": "serpapi", "results_count": len(results), "results": results}


async def _search_google(query: str, num_results: int, provider_cfg: dict) -> Dict[str, Any]:
    """Google Custom Search JSON API"""
    api_key_env = provider_cfg.get("api_key_env", "GOOGLE_API_KEY")
    cx_env = provider_cfg.get("cx_env", "GOOGLE_CX")
    api_key = _get_env(api_key_env)
    cx = _get_env(cx_env)
    if not api_key or not cx:
        return {
            "success": False,
            "error": f"Google API key or CX not configured. Set {api_key_env} and {cx_env} in environment or .env file. "
                     "See https://developers.google.com/custom-search/v1/overview",
        }

    base_url = provider_cfg.get("base_url", "https://www.googleapis.com/customsearch/v1")
    params = {"key": api_key, "cx": cx, "q": query, "num": min(num_results, 10)}

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(base_url, params=params)
        resp.raise_for_status()
        data = resp.json()

    results = []
    for item in data.get("items", [])[:num_results]:
        results.append({
            "title": item.get("title", ""),
            "link": item.get("link", ""),
            "snippet": item.get("snippet", ""),
        })

    return {"success": True, "query": query, "provider": "google", "results_count": len(results), "results": results}


async def _search_duckduckgo(query: str, num_results: int, provider_cfg: dict) -> Dict[str, Any]:
    """DuckDuckGo 搜索（免费，无需 API key），使用 ddgs 库
    
    注意：超时和网络异常不在此处捕获，由 web_search 统一处理重试。
    """
    try:
        from ddgs import DDGS
    except ImportError:
        return {
            "success": False,
            "error": "ddgs library not installed. Run: pip install ddgs",
            "provider": "duckduckgo",
        }

    def _do_search():
        return DDGS().text(query, max_results=num_results)

    # 不捕获异常，让超时/网络异常向上抛出由 web_search 重试
    raw_results = await asyncio.to_thread(_do_search)

    results = []
    for r in raw_results:
        results.append({
            "title": r.get("title", ""),
            "link": r.get("href", ""),
            "snippet": r.get("body", ""),
        })

    if not results:
        return {
            "success": True,
            "query": query,
            "provider": "duckduckgo",
            "results_count": 0,
            "results": [],
            "message": "No results found. Try a different query or use another provider.",
        }

    return {"success": True, "query": query, "provider": "duckduckgo", "results_count": len(results), "results": results}


async def _search_toutiao(query: str, num_results: int, provider_cfg: dict) -> Dict[str, Any]:
    """
    头条搜索（免费，无需 API key）
    流程：
    1. 请求搜索页面获取 HTML
    2. 解析 HTML 提取结果链接
    3. 并发请求前 N 个链接获取页面内容
    4. 提取标题和摘要返回
    """
    base_url = provider_cfg.get("base_url", "https://so.toutiao.com/search")
    max_content_len = provider_cfg.get("max_content_length", 2000)
    params = {"keyword": query}

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        # 第一步：获取搜索结果页面 HTML
        resp = await client.get(base_url, params=params, headers=headers)
        resp.raise_for_status()
        html = resp.text

        # 第二步：解析 HTML 提取链接
        # 提取所有 <a> 标签的 href 和文本
        # 匹配 <a ...href="URL"...>TEXT</a>
        link_pattern = re.compile(
            r'<a\s[^>]*?href=["\']([^"\']+)["\'][^>]*?>(.*?)</a>',
            re.DOTALL | re.IGNORECASE
        )
        # 过滤 HTML 标签用于提取纯文本
        tag_pattern = re.compile(r'<[^>]+>')

        # 收集有效链接（排除 javascript:, #, 站内链接等）
        candidates = []
        seen_urls = set()
        for match in link_pattern.finditer(html):
            href = match.group(1).strip()
            raw_text = match.group(2).strip()
            # 清理文本中的 HTML 标签
            text = tag_pattern.sub('', raw_text).strip()
            text = unescape(text)

            # 过滤无效链接
            if not href or href.startswith(('javascript:', '#', 'mailto:')):
                continue
            if not text or len(text) < 4:
                continue
            # 优先保留头条文章链接
            if href in seen_urls:
                continue
            seen_urls.add(href)

            # 补全相对链接
            if href.startswith('//'):
                href = 'https:' + href
            elif href.startswith('/'):
                href = 'https://so.toutiao.com' + href

            candidates.append({"link": href, "title": text})

            if len(candidates) >= num_results * 3:
                break

        if not candidates:
            return {
                "success": True,
                "query": query,
                "provider": "toutiao",
                "results_count": 0,
                "results": [],
                "message": "No results found. Try a different query or use another provider.",
            }

        # 第三步：并发请求前 N 个链接获取页面内容
        async def _fetch_page(item: dict) -> dict:
            try:
                page_resp = await client.get(item["link"], headers=headers, timeout=10)
                page_resp.raise_for_status()
                page_html = page_resp.text

                # 提取 <title> 标签作为标题（如果之前没有好标题）
                title_match = re.search(r'<title[^>]*?>(.*?)</title>', page_html, re.DOTALL | re.IGNORECASE)
                page_title = unescape(tag_pattern.sub('', title_match.group(1)).strip()) if title_match else item["title"]

                # 提取正文内容：尝试 <article>、<div class="content">、<p> 标签
                content = ""
                # 尝试提取 <p> 标签内容
                p_tags = re.findall(r'<p[^>]*?>(.*?)</p>', page_html, re.DOTALL | re.IGNORECASE)
                if p_tags:
                    paragraphs = []
                    for p in p_tags:
                        p_text = tag_pattern.sub('', p).strip()
                        p_text = unescape(p_text)
                        if len(p_text) > 10:  # 过滤太短的段落
                            paragraphs.append(p_text)
                    content = '\n'.join(paragraphs)

                # 截断内容
                if len(content) > max_content_len:
                    content = content[:max_content_len] + "..."

                return {
                    "title": page_title or item["title"],
                    "link": item["link"],
                    "snippet": content[:500] if content else item["title"],
                }
            except Exception as e:
                logger.warning("Failed to fetch toutiao page", url=item["link"], error=str(e), exc_info=True)
                return {
                    "title": item["title"],
                    "link": item["link"],
                    "snippet": "",
                }

        # 只取前 num_results 个链接去抓取
        to_fetch = candidates[:num_results]
        fetch_tasks = [_fetch_page(item) for item in to_fetch]
        results = await asyncio.gather(*fetch_tasks)

        results = [r for r in results if r.get("title")]

    return {
        "success": True,
        "query": query,
        "provider": "toutiao",
        "results_count": len(results),
        "results": results,
    }


async def _search_playwright(query: str, num_results: int, provider_cfg: dict) -> Dict[str, Any]:
    """
    Playwright 浏览器自动化搜索（模拟点击获取页面内容）
    
    流程：
    1. 使用无头 Playwright 打开 Chrome 浏览器
    2. 打开搜索 URL（base_url + param）
    3. 在搜索结果页面点击结果链接，进入页面获取内容
    4. 关闭内容页面，回到搜索页面继续获取下一个链接
    5. 清理并关闭浏览器
    
    注意：部分搜索引擎（如百度）可能会检测到自动化访问并显示验证码，
    建议使用 Google 或 Bing，或配置代理。
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {
            "success": False,
            "error": "Playwright not installed. Run: pip install playwright",
        }
    
    # 读取配置
    chrome_path = provider_cfg.get("chrome_path", "C:/Program Files/Google/Chrome/Application/chrome.exe")
    headless = provider_cfg.get("headless", True)
    search_engine = provider_cfg.get("search_engine", "baidu")
    search_engines = provider_cfg.get("search_engines", {})
    engine_cfg = search_engines.get(search_engine, {})
    
    if not engine_cfg:
        return {
            "success": False,
            "error": f"Search engine '{search_engine}' not configured in settings.json",
        }
    
    base_url = engine_cfg.get("base_url", "")
    param = engine_cfg.get("param", "q")
    result_selectors = engine_cfg.get("result_selector", ["h3 a"])
    content_selectors = engine_cfg.get("content_selectors", ["article", "main", "body"])
    max_content_length = provider_cfg.get("max_content_length", 3000)
    page_timeout = provider_cfg.get("page_timeout", 30000)
    
    results = []
    playwright = None
    browser = None
    
    try:
        # 启动 Playwright
        playwright = await async_playwright().start()
        
        # 启动浏览器（使用本地 Chrome）
        browser = await playwright.chromium.launch(
            headless=headless,
            executable_path=chrome_path,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        
        # 创建搜索页面 context（添加反检测措施）
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
        )
        
        # 尝试使用 stealth 插件（如果可用）
        try:
            from playwright_stealth import stealth_async
            await stealth_async(context)
            logger.debug("Applied playwright-stealth")
        except ImportError:
            logger.debug("playwright-stealth not available, continuing without it")
        except Exception as e:
            logger.debug("Failed to apply stealth", error=str(e))
        
        search_page = await context.new_page()
        
        # 打开搜索 URL
        search_url = f"{base_url}?{param}={query}"
        logger.info("Opening search URL", url=search_url)
        await search_page.goto(search_url, timeout=page_timeout, wait_until="domcontentloaded")
        
        # 等待搜索结果加载
        await search_page.wait_for_timeout(3000)  # 等待 3 秒让页面加载完成
        
        # 检查是否被重定向到验证页面
        page_title = await search_page.title()
        if "验证" in page_title or "安全" in page_title or "captcha" in page_title.lower():
            logger.warning("Search engine security check detected", title=page_title)
            await context.close()
            return {
                "success": False,
                "error": f"Search engine security check detected (title: {page_title}). "
                         "Try using a different search engine (google/bing) or disable headless mode.",
            }
        
        # 查找搜索结果链接
        result_links = []
        for selector in result_selectors:
            try:
                elements = await search_page.query_selector_all(selector)
                if elements:
                    for elem in elements[:num_results * 2]:  # 多取一些，防止有些无效
                        href = await elem.get_attribute("href")
                        text = await elem.inner_text()
                        if href and text and len(text.strip()) > 3:
                            result_links.append({
                                "title": text.strip(),
                                "link": href,
                                "element": elem,
                            })
                    if result_links:
                        logger.info("Found result links", selector=selector, count=len(result_links))
                        break
            except Exception as e:
                logger.debug("Selector failed, trying next", selector=selector, error=str(e))
                continue
        
        if not result_links:
            await context.close()
            return {
                "success": True,
                "query": query,
                "provider": "playwright",
                "results_count": 0,
                "results": [],
                "message": "No search results found. Try a different query or search engine.",
            }
        
        # 遍历结果链接，逐个点击并获取内容
        for item in result_links[:num_results]:
            content_page = None
            try:
                # 点击链接（在新标签页打开）
                async with context.expect_page() as new_page_info:
                    await item["element"].click(modifiers=["Control"])
                
                content_page = await new_page_info.value
                await content_page.wait_for_load_state("domcontentloaded", timeout=page_timeout)
                await content_page.wait_for_timeout(2000)  # 等待内容加载
                
                # 提取页面内容
                content_text = ""
                for selector in content_selectors:
                    try:
                        elem = await content_page.query_selector(selector)
                        if elem:
                            text = await elem.inner_text()
                            if text and len(text.strip()) > 50:  # 至少 50 字符
                                content_text = text.strip()
                                logger.debug("Content extracted", selector=selector, length=len(content_text))
                                break
                    except Exception as e:
                        logger.debug("Content selector failed", selector=selector, error=str(e))
                        continue
                
                # 截断内容
                if len(content_text) > max_content_length:
                    content_text = content_text[:max_content_length] + "..."
                
                results.append({
                    "title": item["title"],
                    "link": item["link"],
                    "snippet": content_text[:1000] if content_text else item["title"],
                })
                
                # 关闭内容页面
                await content_page.close()
                content_page = None
                
            except Exception as e:
                logger.warning("Failed to get content from link", url=item.get("link"), error=str(e), exc_info=True)
                # 添加无内容的结果
                results.append({
                    "title": item["title"],
                    "link": item["link"],
                    "snippet": "",
                })
                # 确保关闭内容页面
                if content_page:
                    try:
                        await content_page.close()
                    except Exception:
                        pass
        
        # 关闭搜索页面
        await context.close()
        
    except Exception as e:
        # 不捕获超时/连接异常，让 web_search 重试机制处理
        error_str = str(e).lower()
        if any(kw in error_str for kw in ("timeout", "timed out", "connection", "network", "erefused")):
            raise  # 向上抛出触发重试
        logger.error("Playwright search failed", error=str(e), exc_info=True)
        return {
            "success": False,
            "error": f"Playwright search failed: {str(e)}",
        }
    finally:
        # 清理资源
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        if playwright:
            try:
                await playwright.stop()
            except Exception:
                pass
    
    return {
        "success": True,
        "query": query,
        "provider": "playwright",
        "results_count": len(results),
        "results": results,
    }


async def _search_local(query: str, num_results: int, provider_cfg: dict) -> Dict[str, Any]:
    """
    本地 JSON 文件搜索（无需网络，从本地 JSON 文件中检索内容）
    
    JSON 文件格式: [{"title": "...", "link": "...", "content": "..."}, ...]
    支持配置搜索目录，会扫描目录下所有 .json 文件。
    """
    from app.config import _PROJECT_ROOT
    
    # 获取数据目录路径（支持相对路径和绝对路径）
    data_dir_str = provider_cfg.get("data_dir", ".eggs")
    data_dir = Path(data_dir_str)
    if not data_dir.is_absolute():
        data_dir = _PROJECT_ROOT / data_dir
    
    if not data_dir.exists():
        logger.warning("Local search data directory not found", path=str(data_dir))
        return {
            "success": True,
            "query": query,
            "provider": "local",
            "results_count": 0,
            "results": [],
            "message": f"Data directory not found: {data_dir}",
        }
    
    # 查询关键词（转小写匹配）
    query_lower = query.lower()
    query_terms = query_lower.split()
    
    matched_results = []
    json_files = list(data_dir.glob("*.json"))
    
    if not json_files:
        logger.warning("No JSON files found in data directory", path=str(data_dir))
        return {
            "success": True,
            "query": query,
            "provider": "local",
            "results_count": 0,
            "results": [],
            "message": f"No JSON files in: {data_dir}",
        }
    
    for json_file in json_files:
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to read local JSON file", file=str(json_file), error=str(e))
            continue
        
        # 支持 JSON 为列表或字典（字典时取 values 中的列表）
        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            # 尝试找第一个列表值
            for v in data.values():
                if isinstance(v, list):
                    items = v
                    break
        
        for item in items:
            if not isinstance(item, dict):
                continue
            
            title = item.get("title", "")
            content = item.get("content", "") or item.get("description", "") or item.get("snippet", "")
            link = item.get("link", "") or item.get("url", "") or item.get("href", "")
            
            # 简单关键词匹配：任一关键词出现在 title 或 content 中
            text_to_search = f"{title} {content}".lower()
            if any(term in text_to_search for term in query_terms):
                matched_results.append({
                    "title": title,
                    "link": link,
                    "snippet": content,
                    "source_file": json_file.name,
                })
                
                if len(matched_results) >= num_results:
                    break
        
        if len(matched_results) >= num_results:
            break
    
    if not matched_results:
        return {
            "success": True,
            "query": query,
            "provider": "local",
            "results_count": 0,
            "results": [],
            "message": f"No results found in local files. Scanned {len(json_files)} JSON file(s).",
        }
    
    logger.info("Local search completed", query=query, results=len(matched_results), files_scanned=len(json_files))
    return {
        "success": True,
        "query": query,
        "provider": "local",
        "results_count": len(matched_results),
        "results": matched_results,
    }


# 提供商路由映射
_SEARCH_PROVIDERS = {
    "brave": _search_brave,
    "serpapi": _search_serpapi,
    "google": _search_google,
    "duckduckgo": _search_duckduckgo,
    "toutiao": _search_toutiao,
    "playwright": _search_playwright,
    "local": _search_local,
}


async def web_search(query: str, num_results: int = 5) -> Dict[str, Any]:
    """
    执行网络搜索，支持多个搜索提供商。
    默认使用 Brave Search（免费层 2000 次/月）。
    可在 .happy-claw/settings.json 的 tools.web_search 中切换提供商。
    
    支持的提供商：
        - brave: Brave Search API（默认，免费）
        - duckduckgo: DuckDuckGo（免费，无需 API key）
        - toutiao: 头条搜索（免费，无需 API key，解析 HTML 结果）
        - playwright: Playwright 浏览器自动化（模拟点击获取页面内容）
        - local: 本地 JSON 文件搜索（无需网络，从本地文件检索）
        - serpapi: SerpAPI Google 搜索（需付费）
        - google: Google Custom Search（需配置 API key 和 CX）
    
    内置超时重试机制：超时或网络错误时自动重试，最多 3 次，指数退避。
    
    Args:
        query: 搜索关键词
        num_results: 返回结果数量（1-10）
    
    Returns:
        搜索结果列表，包含标题、链接、摘要
    """
    num_results = max(1, min(num_results, 10))
    logger.info("Executing web search", query=query, num_results=num_results)

    # 加载配置
    ws_config = _load_web_search_config()
    provider_name = ws_config.get("provider", "brave")
    providers_cfg = ws_config.get("providers", {})
    provider_cfg = providers_cfg.get(provider_name, {})

    # 重试配置
    max_retries = provider_cfg.get("max_retries", ws_config.get("max_retries", 3))
    retry_base_delay = provider_cfg.get("retry_base_delay", ws_config.get("retry_base_delay", 1))

    # 查找提供商处理器
    search_fn = _SEARCH_PROVIDERS.get(provider_name)
    if not search_fn:
        available = ", ".join(_SEARCH_PROVIDERS.keys())
        return {
            "success": False,
            "error": f"Unknown search provider: '{provider_name}'. Available: {available}",
        }

    # 可重试的异常类型
    retryable_error_types = (
        httpx.TimeoutException,
        httpx.ConnectError,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        asyncio.TimeoutError,
        ConnectionError,
        OSError,
    )
    # 用于匹配非标准异常消息中的超时/网络关键词
    _retry_keywords = ("timeout", "timed out", "connection", "network", "erefused", "temporarily unavailable")

    def _is_retryable(e: Exception) -> bool:
        """判断异常是否可重试（支持 httpx/ddgs/playwright 等不同库的异常）"""
        if isinstance(e, retryable_error_types):
            return True
        # 检查异常消息中的关键词（覆盖 Playwright、ddgs 等非 httpx 异常）
        msg = str(e).lower()
        return any(kw in msg for kw in _retry_keywords)

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            result = await search_fn(query, num_results, provider_cfg)
            logger.info(
                "Web search completed",
                provider=provider_name,
                success=result.get("success"),
                results_count=result.get("results_count", 0),
                attempt=attempt,
            )
            return result

        except Exception as e:
            if _is_retryable(e):
                last_error = e
                error_type = type(e).__name__
                if attempt < max_retries:
                    delay = retry_base_delay * (2 ** (attempt - 1))  # 指数退避: 1s, 2s, 4s
                    logger.warning(
                        "Search timeout/network error, retrying",
                        provider=provider_name,
                        attempt=attempt,
                        max_retries=max_retries,
                        delay=delay,
                        error_type=error_type,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        "Search failed after all retries",
                        provider=provider_name,
                        attempts=attempt,
                        error_type=error_type,
                        error=str(e),
                    )
            elif isinstance(e, httpx.HTTPStatusError):
                logger.error("Search API HTTP error", provider=provider_name, status=e.response.status_code)
                return {
                    "success": False,
                    "error": f"Search API returned HTTP {e.response.status_code}",
                    "provider": provider_name,
                }
            elif isinstance(e, httpx.RequestError):
                logger.error("Search API request error", provider=provider_name, error=str(e), exc_info=True)
                return {
                    "success": False,
                    "error": f"Search API request failed: {str(e)}",
                    "provider": provider_name,
                }
            else:
                logger.error("Web search failed", provider=provider_name, error=str(e), exc_info=True)
                return {
                    "success": False,
                    "error": str(e),
                    "provider": provider_name,
                }

    # 所有重试均失败
    return {
        "success": False,
        "error": f"Search failed after {max_retries} attempts: {str(last_error)}",
        "provider": provider_name,
        "attempts": max_retries,
    }


# ===== Bash Command 工具 =====

def _detect_shell() -> str:
    """检测当前操作系统并返回合适的 shell 命令"""
    system = platform.system().lower()
    
    if system == "windows":
        # Windows 系统使用 PowerShell
        return "powershell"
    elif system in ["linux", "darwin"]:
        # Linux/macOS 使用 bash
        return "bash"
    else:
        # 默认使用 bash
        return "bash"


async def bash_command(command: str, timeout_seconds: int = 30, run_in_background: bool = False) -> Dict[str, Any]:
    """
    执行 Bash/PowerShell 命令
    
    ⚠️ 安全警告：此工具允许执行任意系统命令，请谨慎使用！
    
    Args:
        command: 要执行的命令
        timeout_seconds: 超时时间（秒），默认30秒
        run_in_background: 是否在后台运行（立即返回，结果稍后注入）
    
    Returns:
        命令执行结果，包含 stdout、stderr、return_code
    
    Environment Info:
        - Operating System: {platform.system()}
        - Shell: {_detect_shell()}
        - Current Directory: {os.getcwd()}
        - User: {os.getlogin() if hasattr(os, 'getlogin') else 'unknown'}
        
        请根据上述环境信息选择合适的命令语法：
        - Windows: 使用 PowerShell 语法（如 Get-ChildItem、Select-String 等）
        - Linux/macOS: 使用 Bash 语法（如 ls、grep、find 等）
    
    Security:
        危险命令检测已通过 prehook 实现，配置在 .happy-claw/settings.json 中
    """
    logger.info("Executing bash command", command=command, timeout=timeout_seconds, run_in_background=run_in_background)
    
    # 后台运行模式
    if run_in_background:
        from app.core.background_jobs import background_job_manager
        
        if background_job_manager.disabled:
            # 后台任务被禁用，回退到同步执行
            logger.info("Background tasks disabled, falling back to sync execution")
            run_in_background = False
        else:
            # 获取当前会话 ID
            session_id = "unknown"
            try:
                _, _, _, caller_self = _get_parent_agent_info()
                session_id = getattr(caller_self, '_current_conversation', None) or "unknown"
            except Exception:
                pass
            
            try:
                job = await background_job_manager.launch_bash_background(
                    command=command,
                    session_id=session_id,
                    tool_use_id=current_tool_call_id.get(),
                    cwd=_tool_cwd(),
                    timeout_seconds=timeout_seconds,
                )
                return {
                    "success": True,
                    "async_launched": True,
                    "job_id": job.job_id,
                    "message": f"Background task launched. Job ID: {job.job_id}. Results will be injected when complete.",
                    "output_file": str(job.output_file),
                }
            except Exception as e:
                logger.error("Failed to launch background task", error=str(e), exc_info=True)
                return {
                    "success": False,
                    "error": f"Failed to launch background task: {str(e)}",
                }
    
    # 同步执行模式（原有逻辑）
    # 检测 shell
    shell = _detect_shell()
    system = platform.system().lower()
    
    try:
        # 构建命令
        if system == "windows":
            # Windows PowerShell
            cmd_args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command]
        else:
            # Linux/macOS Bash
            cmd_args = ["bash", "-c", command]
        
        # 执行命令
        process = await asyncio.create_subprocess_exec(
            *cmd_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=_tool_cwd(),
        )
        
        # 等待命令完成（带超时）
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            process.kill()
            return {
                "success": False,
                "error": f"Command timed out after {timeout_seconds} seconds",
                "stdout": "",
                "stderr": "Timeout",
                "return_code": -1,
            }
        
        # 解码输出
        stdout_str = stdout.decode('utf-8', errors='replace').strip()
        stderr_str = stderr.decode('utf-8', errors='replace').strip()
        
        success = process.returncode == 0
        
        logger.info(
            "Command executed",
            command=command,
            success=success,
            return_code=process.returncode,
        )
        
        return {
            "success": success,
            "command": command,
            "shell": shell,
            "stdout": stdout_str,
            "stderr": stderr_str,
            "return_code": process.returncode,
        }
        
    except Exception as e:
        logger.error("Command execution failed", command=command, error=str(e), exc_info=True)
        return {
            "success": False,
            "error": str(e),
            "stdout": "",
            "stderr": str(e),
            "return_code": -1,
        }


# ===== 文件读取工具 =====

async def read_file(file_path: str, max_lines: int = 1000) -> Dict[str, Any]:
    """
    读取文件内容
    
    Args:
        file_path: 文件路径（相对或绝对路径）
        max_lines: 最大读取行数，防止读取过大文件
    
    Returns:
        文件内容、行数、文件大小等信息
    """
    logger.info("Reading file", file_path=file_path)
    
    try:
        # 转换为绝对路径（相对路径基于当前工作区解析，绝对路径不受限）
        abs_path = resolve_ws_path(file_path)
        
        # 检查文件是否存在
        if not os.path.exists(abs_path):
            return {
                "success": False,
                "error": f"File not found: {abs_path}",
            }
        
        # 检查是否是文件
        if not os.path.isfile(abs_path):
            return {
                "success": False,
                "error": f"Not a file: {abs_path}",
            }
        
        # 读取文件
        with open(abs_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = []
            for i, line in enumerate(f):
                if i >= max_lines:
                    lines.append(f"\n... (truncated after {max_lines} lines)")
                    break
                lines.append(line)
        
        content = ''.join(lines)
        file_size = os.path.getsize(abs_path)
        
        return {
            "success": True,
            "file_path": abs_path,
            "content": content,
            "lines_read": min(len(lines), max_lines),
            "file_size_bytes": file_size,
            "truncated": len(lines) > max_lines,
        }
        
    except Exception as e:
        logger.error("Failed to read file", file_path=file_path, error=str(e), exc_info=True)
        return {
            "success": False,
            "error": str(e),
        }


# ===== 文件写入工具 =====

async def _acquire_memory_write_lock(abs_path: str):
    """
    写入目标位于记忆目录内时，获取统一记忆写入锁。

    - 后台任务上下文（提取/整理）：任务级锁应已持有，仅验证未被抢占；
      被主 Agent 强制抢占时拒绝写入，避免并发冲突。
    - 主 Agent（无上下文）：优先级最高，短暂重试后强制获取。
    - 非记忆目录/记忆未启用：不加锁。

    Returns:
        (holder_used, error_message)，holder_used 为 None 表示未加锁或失败，
        error_message 非 None 表示应拒绝写入。
    """
    try:
        from app.core.memory_system.store import (
            memory_store, current_memory_lock_holder, LOCK_HOLDER_MAIN_AGENT,
        )
    except Exception:
        return None, None

    if not memory_store.enabled or not memory_store.is_within_memory_root(abs_path):
        return None, None

    ctx_holder = current_memory_lock_holder.get()
    if ctx_holder:
        # 后台任务（提取/整理）：验证任务级锁仍由自己持有（未被抢占）
        if memory_store.get_memory_lock_holder() == ctx_holder:
            return ctx_holder, None
        current = memory_store.get_memory_lock_holder() or "unknown"
        return None, (
            f"Memory directory write lock is currently held by '{current}'. "
            "This write was rejected to avoid conflicts. Please skip this write."
        )

    # 主 Agent：优先级最高，短暂重试后强制获取（避免阻塞用户交互）
    for _ in range(3):
        if memory_store.try_acquire_memory_lock(LOCK_HOLDER_MAIN_AGENT):
            return LOCK_HOLDER_MAIN_AGENT, None
        await asyncio.sleep(0.5)
    if memory_store.force_acquire_memory_lock(LOCK_HOLDER_MAIN_AGENT):
        return LOCK_HOLDER_MAIN_AGENT, None
    return None, "Failed to acquire memory write lock."


async def write_file(file_path: str, content: str, append: bool = False) -> Dict[str, Any]:
    """
    写入文件内容
    
    Args:
        file_path: 文件路径
        content: 要写入的内容
        append: 是否追加模式（False 为覆盖）
    
    Returns:
        写入结果
    """
    logger.info("Writing file", file_path=file_path, append=append)
    
    lock_holder_used: Optional[str] = None
    try:
        abs_path = resolve_ws_path(file_path)
        
        # 记忆目录写入锁保护（主 Agent / 提取 / 整理三方互斥）
        lock_holder_used, lock_error = await _acquire_memory_write_lock(abs_path)
        if lock_error:
            logger.warning("Memory write rejected by lock", file_path=abs_path, error=lock_error)
            return {"success": False, "error": lock_error}
        
        # 确保目录存在
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        
        mode = 'a' if append else 'w'
        with open(abs_path, mode, encoding='utf-8') as f:
            f.write(content)
        
        file_size = os.path.getsize(abs_path)
        
        return {
            "success": True,
            "file_path": abs_path,
            "bytes_written": len(content.encode('utf-8')),
            "file_size_bytes": file_size,
            "appended": append,
        }
        
    except Exception as e:
        logger.error("Failed to write file", file_path=file_path, error=str(e), exc_info=True)
        return {
            "success": False,
            "error": str(e),
        }
    finally:
        # 仅主 Agent 的写入级锁在此释放；后台任务的任务级锁由任务调用方释放
        if lock_holder_used:
            try:
                from app.core.memory_system.store import memory_store, LOCK_HOLDER_MAIN_AGENT
                if lock_holder_used == LOCK_HOLDER_MAIN_AGENT:
                    memory_store.release_memory_lock(LOCK_HOLDER_MAIN_AGENT)
            except Exception as e:
                logger.debug("Memory lock release skipped", error=str(e))


# ===== 文件编辑工具（精确字符串替换） =====

async def edit_file(
    file_path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> Dict[str, Any]:
    """
    对已有文件进行精确的字符串替换。
    基于 old_string → new_string 的查找替换，默认要求 old_string 在文件中唯一出现，
    以确保修改精准、避免幻觉。设置 replace_all=True 可替换所有匹配项。

    Args:
        file_path: 目标文件路径（相对或绝对）
        old_string: 要被替换的原始字符串
        new_string: 替换后的新字符串
        replace_all: 是否替换所有匹配项（默认 false，要求唯一匹配）

    Returns:
        替换结果，包含修改的文件路径、替换次数等信息
    """
    logger.info("Editing file", file_path=file_path, replace_all=replace_all)

    try:
        abs_path = resolve_ws_path(file_path)

        if not os.path.exists(abs_path):
            return {"success": False, "error": f"File not found: {abs_path}"}
        if not os.path.isfile(abs_path):
            return {"success": False, "error": f"Not a file: {abs_path}"}
        if not old_string:
            return {"success": False, "error": "old_string must not be empty"}

        # 读取文件内容
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        # 统计匹配次数
        count = content.count(old_string)
        if count == 0:
            return {
                "success": False,
                "error": f"old_string not found in {abs_path}. "
                         "Make sure the text matches exactly (including whitespace and indentation).",
            }
        if count > 1 and not replace_all:
            return {
                "success": False,
                "error": f"old_string found {count} times in {abs_path}. "
                         "The match must be unique. Provide more context to make it unique, "
                         "or set replace_all=true to replace all occurrences.",
            }

        # 执行替换
        if replace_all:
            new_content = content.replace(old_string, new_string)
        else:
            new_content = content.replace(old_string, new_string, 1)

        # 写回文件
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        replacements_made = count if replace_all else 1
        return {
            "success": True,
            "file_path": abs_path,
            "replacements": replacements_made,
            "file_size_bytes": len(new_content.encode("utf-8")),
        }

    except Exception as e:
        logger.error("Failed to edit file", file_path=file_path, error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


# ===== Glob 文件搜索工具 =====

async def glob_search(
    pattern: str,
    path: str = ".",
    max_results: int = 100,
) -> Dict[str, Any]:
    """
    根据 glob 模式按文件名或路径查找文件。
    搜索结果默认按修改时间排序，最近修改的文件排在前面。

    Args:
        pattern: Glob 模式（如 ** /*.ts, src/ *.py）
        path: 搜索根目录（默认当前目录）
        max_results: 最大返回结果数（默认 100）

    Returns:
        匹配的文件列表，按修改时间降序排列
    """
    logger.info("Glob search", pattern=pattern, path=path)

    try:
        root = resolve_ws_path(path)
        if not os.path.isdir(root):
            return {"success": False, "error": f"Directory not found: {root}"}

        # 使用 recursive glob
        full_pattern = os.path.join(root, pattern)
        matches = glob_module.glob(full_pattern, recursive=True)

        # 仅保留文件（排除目录）
        file_matches = [m for m in matches if os.path.isfile(m)]

        # 按修改时间降序排序（最近修改在前）
        file_matches.sort(key=lambda f: os.path.getmtime(f), reverse=True)

        # 截断
        truncated = len(file_matches) > max_results
        file_matches = file_matches[:max_results]

        return {
            "success": True,
            "pattern": pattern,
            "root": root,
            "files": file_matches,
            "total_matches": len(file_matches),
            "truncated": truncated,
        }

    except Exception as e:
        logger.error("Glob search failed", pattern=pattern, error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


# ===== Grep 内容搜索工具 =====

async def grep_search(
    pattern: str,
    path: str = ".",
    include: str = "",
    case_sensitive: bool = True,
    max_results: int = 100,
    context_lines: int = 0,
    output_mode: str = "content",
) -> Dict[str, Any]:
    """
    使用正则表达式在文件内容中进行搜索，基于 ripgrep (rg) 实现，速度极快。
    如果 rg 不可用则回退到 Python 内置实现。

    Args:
        pattern: 正则表达式搜索模式
        path: 搜索根目录或文件路径
        include: 文件名过滤 glob 模式（如 *.py, *.ts）
        case_sensitive: 是否区分大小写（默认 true）
        max_results: 最大返回结果数（默认 100）
        context_lines: 匹配行前后显示的上下文行数（默认 0）
        output_mode: 输出模式 - "files_with_matches" 仅列出匹配文件名，
                     "content" 显示匹配行及上下文（默认 "content"）

    Returns:
        搜索结果
    """
    logger.info("Grep search", pattern=pattern, path=path, output_mode=output_mode)

    try:
        abs_path = resolve_ws_path(path)
        if not os.path.exists(abs_path):
            return {"success": False, "error": f"Path not found: {abs_path}"}

        # 尝试使用 ripgrep (rg)
        rg_available = False
        try:
            probe = subprocess.run(
                ["rg", "--version"],
                capture_output=True, timeout=5,
            )
            rg_available = (probe.returncode == 0)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        if rg_available:
            return await _grep_with_rg(
                pattern, abs_path, include, case_sensitive,
                max_results, context_lines, output_mode,
            )
        else:
            return await _grep_fallback(
                pattern, abs_path, include, case_sensitive,
                max_results, context_lines, output_mode,
            )

    except Exception as e:
        logger.error("Grep search failed", pattern=pattern, error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


async def _grep_with_rg(
    pattern: str, abs_path: str, include: str,
    case_sensitive: bool, max_results: int,
    context_lines: int, output_mode: str,
) -> Dict[str, Any]:
    """使用 ripgrep 执行搜索"""
    cmd = ["rg", "--json"]
    if not case_sensitive:
        cmd.append("-i")
    if include:
        cmd.extend(["--glob", include])
    if context_lines > 0:
        cmd.extend(["-C", str(context_lines)])
    if output_mode == "files_with_matches":
        cmd.append("--files-with-matches")

    cmd.extend([pattern, abs_path])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

    # rg 返回 0=有匹配, 1=无匹配, 2=错误
    if proc.returncode == 2:
        err_msg = stderr.decode("utf-8", errors="replace").strip()
        return {"success": False, "error": f"rg error: {err_msg}"}

    if output_mode == "files_with_matches":
        files = stdout.decode("utf-8", errors="replace").strip().splitlines()[:max_results]
        return {
            "success": True,
            "pattern": pattern,
            "output_mode": "files_with_matches",
            "files": files,
            "total_matches": len(files),
        }

    # 解析 JSON 输出
    results = []
    for line in stdout.decode("utf-8", errors="replace").strip().splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "match":
            data = obj.get("data", {})
            results.append({
                "file": data.get("path", {}).get("text", ""),
                "line_number": data.get("line_number", 0),
                "text": data.get("lines", {}).get("text", "").rstrip("\n"),
            })
        elif obj.get("type") == "context" and context_lines > 0:
            data = obj.get("data", {})
            results.append({
                "file": data.get("path", {}).get("text", ""),
                "line_number": data.get("line_number", 0),
                "text": data.get("lines", {}).get("text", "").rstrip("\n"),
                "is_context": True,
            })
        if len(results) >= max_results:
            break

    return {
        "success": True,
        "pattern": pattern,
        "output_mode": output_mode,
        "results": results,
        "total_matches": len(results),
    }


async def _grep_fallback(
    pattern: str, abs_path: str, include: str,
    case_sensitive: bool, max_results: int,
    context_lines: int, output_mode: str,
) -> Dict[str, Any]:
    """Python 内置实现的 grep 回退方案"""
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        return {"success": False, "error": f"Invalid regex: {e}"}

    # 确定要搜索的文件列表
    if os.path.isfile(abs_path):
        files_to_search = [abs_path]
    else:
        files_to_search = []
        for root_dir, _dirs, filenames in os.walk(abs_path):
            for fname in filenames:
                full = os.path.join(root_dir, fname)
                if include and not fnmatch.fnmatch(fname, include):
                    continue
                files_to_search.append(full)

    # 仅列出匹配文件名
    if output_mode == "files_with_matches":
        matched_files = []
        for fpath in files_to_search:
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if regex.search(line):
                            matched_files.append(fpath)
                            break
            except (OSError, UnicodeDecodeError):
                continue
            if len(matched_files) >= max_results:
                break
        return {
            "success": True,
            "pattern": pattern,
            "output_mode": "files_with_matches",
            "files": matched_files,
            "total_matches": len(matched_files),
        }

    # 显示匹配行及上下文
    results = []
    for fpath in files_to_search:
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except (OSError, UnicodeDecodeError):
            continue

        for i, line in enumerate(lines):
            if regex.search(line):
                results.append({
                    "file": fpath,
                    "line_number": i + 1,
                    "text": line.rstrip("\n"),
                })
                # 添加上下文
                if context_lines > 0:
                    start = max(0, i - context_lines)
                    end = min(len(lines), i + context_lines + 1)
                    for ci in range(start, i):
                        results.append({
                            "file": fpath,
                            "line_number": ci + 1,
                            "text": lines[ci].rstrip("\n"),
                            "is_context": True,
                        })
                    for ci in range(i + 1, end):
                        results.append({
                            "file": fpath,
                            "line_number": ci + 1,
                            "text": lines[ci].rstrip("\n"),
                            "is_context": True,
                        })
                if len(results) >= max_results:
                    break
        if len(results) >= max_results:
            break

    return {
        "success": True,
        "pattern": pattern,
        "output_mode": output_mode,
        "results": results[:max_results],
        "total_matches": len(results[:max_results]),
    }


# ===== Todo Write 工具（系统工具） =====

async def todo_write(todos: list) -> Dict[str, Any]:
    """
    创建和管理当前任务会话的任务列表。
    用于在大型任务中跟踪步骤进度，避免上下文过长时遗忘后续步骤。
    
    Args:
        todos: 任务列表，每个任务包含 content（任务描述）和 status（状态：pending/in_progress/completed）
    
    Returns:
        当前任务列表的结构化数据
    """
    logger.info("Todo list updated", todos_count=len(todos))
    
    # 为每个任务添加 ID 并返回结构化数据
    formatted_todos = []
    for i, todo in enumerate(todos):
        formatted_todos.append({
            "id": str(i + 1),
            "content": todo.get("content", ""),
            "status": todo.get("status", "pending"),
        })
    
    # 生成可读的任务列表摘要
    status_icons = {"pending": "○", "in_progress": "◐", "completed": "●"}
    lines = ["Current task list:"]
    for todo in formatted_todos:
        icon = status_icons.get(todo["status"], "○")
        lines.append(f"  {icon} [{todo['status'].upper()}] {todo['content']}")
    
    pending_count = sum(1 for t in formatted_todos if t["status"] == "pending")
    in_progress_count = sum(1 for t in formatted_todos if t["status"] == "in_progress")
    completed_count = sum(1 for t in formatted_todos if t["status"] == "completed")
    lines.append(f"\nProgress: {completed_count}/{len(formatted_todos)} completed, {in_progress_count} in progress, {pending_count} pending")
    
    return {
        "success": True,
        "todos": formatted_todos,
        "summary": "\n".join(lines),
    }


# ===== Spawn Agent 工具（系统工具） =====

def _get_parent_agent_info():
    """从调用上下文中获取父agent信息
    
    遍历整个调用栈，找到所有匹配的 agent，选择层级最高的（level 最大的）。
    这样可以确保找到直接调用 spawn 工具的 agent，而不是更上层的 agent。
    """
    import inspect
    frame = inspect.currentframe()
    
    # 收集所有匹配的 agent
    candidates = []
    for i in range(15):
        frame = frame.f_back
        if frame is None:
            break
        if 'self' in frame.f_locals:
            caller_self = frame.f_locals['self']
            if hasattr(caller_self, 'id') and hasattr(caller_self, '_current_conversation'):
                # 注意：不能用 `or -1`，因为 level=0 是有效值
                parent_level = getattr(caller_self, 'level', -1)
                if parent_level is None:
                    parent_level = -1
                parent_name = getattr(caller_self.config, 'name', 'unknown') if hasattr(caller_self, 'config') else 'unknown'
                candidates.append({
                    'self': caller_self,
                    'level': parent_level,
                    'name': parent_name,
                    'id': caller_self.id,
                    'conversation': caller_self._current_conversation or "default",
                    'frame_index': i,
                })
    
    if not candidates:
        raise RuntimeError("Cannot determine parent agent context")
    
    # 选择层级最高的（level 最大的），如果 level 相同则选择 frame_index 最小的（最接近当前帧的）
    best = max(candidates, key=lambda c: (c['level'], -c['frame_index']))
    
    return best['id'], best['conversation'], best['level'], best['self']


async def spawn_agent(
    tasks: list,
) -> Dict[str, Any]:
    """
    生成子agent来执行任务。支持单个或多个子agent，自动处理依赖关系和并发执行。
    无依赖的子agent会并发执行，有依赖的会等待依赖完成后再执行。
    子agent完成后结果会自动通知父agent。
    
    Args:
        tasks: 任务列表，每个任务为对象:
            - subagent_name (str): 子agent名称（skill类型时为skill名称）
            - subagent_role (str, 可选): 角色描述，定义其行为模式和专业领域（skill类型时无需填写）
            - task_description (str): 要分配给子agent的具体任务描述
            - agent_type (str, 可选): agent类型，"common"(默认，普通指派) 或 "skill"(skill指派)
            - depends_on (list[str], 可选): 依赖的其他子agent名称列表
            - allowed_tools (list[str], 可选): 允许子agent使用的工具列表
            - config_overrides (object, 可选): 配置覆盖项，可指定 llm_model、temperature 等
    
    Returns:
        包含各子agent执行状态和 DAG 可视化数据的字典
    
    Example:
        ```json
        {
            "tasks": [
                {
                    "subagent_name": "Researcher",
                    "subagent_role": "Web research expert",
                    "task_description": "Search for latest AI trends",
                    "depends_on": []
                },
                {
                    "subagent_name": "Writer",
                    "subagent_role": "Content writer",
                    "task_description": "Write article based on research",
                    "depends_on": ["Researcher"]
                }
            ]
        }
        ```
        
        Skill type example:
        ```json
        {
            "tasks": [
                {
                    "subagent_name": "web-search",
                    "agent_type": "skill",
                    "task_description": "Search for latest AI trends"
                }
            ]
        }
        ```
    """
    logger.info("Spawning subagent(s)", tasks_count=len(tasks))
    
    try:
        parent_agent_id, parent_conversation_id, parent_level, caller_self = _get_parent_agent_info()
        
        from app.core.a2a_protocol import SpawnRequest
        from app.core.subagent_manager import subagent_manager
        from app.services.llm_service import create_llm_service
        from app.tools.registry import tool_registry
        from app.core.memory import create_memory_manager
        from app.core.prompt_engine import create_prompt_engine
        
        level = parent_level + 1
        dag_id = f"dag-{str(uuid.uuid4())}"
        
        # 构建 SpawnRequest 列表，并建立名称到 spawn_uuid 的映射
        requests = []
        name_to_uuid: Dict[str, str] = {}
        
        for task_item in tasks:
            spawn_uuid = f"{level}-{str(uuid.uuid4())}"
            name_to_uuid[task_item["subagent_name"]] = spawn_uuid
            
            agent_type = task_item.get("agent_type", "common")
            subagent_name = task_item["subagent_name"]
            subagent_role = task_item.get("subagent_role", "")
            task_description = task_item["task_description"]
            
            # skill 类型：自动填充 role 和 task_description
            if agent_type == "skill":
                from app.skills import skill_loader
                # 验证 skill 存在
                skill_entry = skill_loader.get_skill(subagent_name)
                if not skill_entry:
                    raise ValueError(
                        f"Skill '{subagent_name}' not found. Available: {', '.join(skill_loader.list_enabled_skill_names()) or 'none'}"
                    )
                # subagent_role 填充为 subagent_name
                subagent_role = subagent_name
                # task_description 拼接 skill body
                skill_body = skill_loader.get_skill_body(subagent_name)
                task_description = (
                    f"{task_description}\n\n"
                    f"Please use the '{subagent_name}' skill to handle this task. "
                    f"Follow the skill instructions below:\n\n{skill_body}"
                )
            
            # 解析依赖：将名称转换为 spawn_uuid
            depends_on_names = task_item.get("depends_on", [])
            depends_on_uuids = []
            for dep_name in depends_on_names:
                if dep_name in name_to_uuid:
                    depends_on_uuids.append(name_to_uuid[dep_name])
                else:
                    # 依赖的名称还没处理，先记录占位符，后面再解析
                    depends_on_uuids.append(f"__pending__:{dep_name}")
            
            request = SpawnRequest(
                parent_agent_id=parent_agent_id,
                parent_agent_name=caller_self.config.name if hasattr(caller_self, 'config') else None,
                parent_conversation_id=parent_conversation_id,
                spawn_uuid=spawn_uuid,
                level=level,
                subagent_name=subagent_name,
                subagent_role=subagent_role,
                task_description=task_description,
                allowed_tools=task_item.get("allowed_tools", []),
                config_overrides=task_item.get("config_overrides"),
                depends_on=depends_on_uuids,
                agent_type=agent_type,
            )
            requests.append(request)
        
        # 二次解析：将 __pending__:name 替换为实际的 spawn_uuid
        for req in requests:
            resolved_deps = []
            for dep in req.depends_on:
                if dep.startswith("__pending__:"):
                    dep_name = dep[len("__pending__:"):]
                    if dep_name in name_to_uuid:
                        resolved_deps.append(name_to_uuid[dep_name])
                    else:
                        logger.warning("Dependency not found", name=dep_name)
                else:
                    resolved_deps.append(dep)
            req.depends_on = resolved_deps
        
        # 注册结果回调：子agent完成后自动注入结果到父agent对话
        for req in requests:
            async def _on_result(dep_result, _req=req):
                if hasattr(caller_self, 'receive_subagent_result'):
                    await caller_self.receive_subagent_result(dep_result)
            subagent_manager.register_result_callback(
                parent_agent_id, req.spawn_uuid, _on_result
            )
        
        # 创建执行服务
        llm_service = create_llm_service()
        memory_manager = create_memory_manager()
        prompt_engine = create_prompt_engine()
        
        # 检查是否有活跃的共享 DAG（嵌套调用时复用父级 DAG）
        from app.core.subagent_manager import _active_dag
        active_dag = _active_dag.get()
        
        # 统一通过 execute_batch 执行（自动处理依赖和并发）
        results, graph = await subagent_manager.execute_batch(
            requests=requests,
            llm_service=llm_service,
            tool_registry=tool_registry,
            memory_manager=memory_manager,
            prompt_engine=prompt_engine,
            dag_id=dag_id,
            conversation_id=parent_conversation_id,
            existing_dag=active_dag,
        )
        
        # 构建返回结果
        task_results = []
        for r in results:
            task_results.append({
                "spawn_uuid": r.spawn_uuid,
                "subagent_id": r.subagent_id,
                "success": r.success,
                "result": r.result,
                "error_message": r.error_message,
                "execution_time_seconds": r.execution_time_seconds,
            })
        
        final_dag = graph.to_dict()
        
        return {
            "success": True,
            "total_tasks": len(tasks),
            "completed": sum(1 for r in results if r.success),
            "failed": sum(1 for r in results if not r.success),
            "results": task_results,
            "dag": final_dag,
        }
        
    except Exception as e:
        logger.error("Failed to spawn subagent(s)", error=str(e), exc_info=True)
        return {
            "success": False,
            "error": str(e),
            "message": "Exception occurred while spawning subagent(s)",
        }


# ===== Load Skill 工具（系统工具） =====

async def load_skill(name: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    加载指定技能的主体内容。
    
    技能系统提供可扩展的指令集，调用此工具后将获得该技能的完整使用说明。
    仅在需要执行特定技能时调用，不需要时不要调用。
    
    Args:
        name: 技能名称（必须与已注册的技能名称完全匹配）
        args: 可选参数，传递给技能的参数字典（具体参数格式见技能说明）
    
    Returns:
        技能的主体内容（Markdown 格式的指令文本）
    """
    logger.info("Loading skill", name=name, args=args)
    
    try:
        from app.skills import skill_loader
        
        body = skill_loader.get_skill_body(name, args)
        
        return {
            "success": True,
            "skill_name": name,
            "content": body,
        }
    except ValueError as e:
        return {
            "success": False,
            "error": str(e),
        }
    except Exception as e:
        logger.error("Failed to load skill", name=name, error=str(e), exc_info=True)
        return {
            "success": False,
            "error": f"Failed to load skill '{name}': {str(e)}",
        }


# ===== 任务系统工具（文件型任务: TaskCreate/TaskGet/TaskUpdate/TaskList） =====

def _resolve_task_list_id_from_context() -> Optional[str]:
    """解析当前调用上下文的任务列表 ID

    优先级:
        环境变量 HAPPY_CLAW_TASK_LIST_ID
        > 团队 ContextVar current_team_id（团队模式，队友/Lead 当前请求）
        > 团队后备映射（队友 metadata.team_id / Lead 会话 -> team_id）
        > 父会话 id（子agent时）或当前会话 id
        > None（由 store 兑底 default）

    团队模式下，任务以团队 ID 作为 task_list_id 落到 .happy-claw/tasks/{team-id}/。
    子 agent 的会话是临时的，任务应归属父会话，避免子 agent 退出后任务成为孤儿。
    """
    env_val = os.environ.get("HAPPY_CLAW_TASK_LIST_ID", "").strip()
    if env_val:
        return env_val
    # ✅ 团队模式：ContextVar 优先（当前请求内由 spawn/队友循环设置）
    try:
        from app.core.team_system.models import current_team_id
        tid = current_team_id.get()
        if tid:
            return tid
    except Exception:
        pass
    try:
        _, _, _, caller_self = _get_parent_agent_info()
        # ✅ 团队后备映射（ContextVar 跨请求不持久，按调用者自身作用域兜底）
        try:
            from app.core.team_system.manager import team_manager
            # 队友：实例创建时 metadata 已写入 team_id，精确匹配（不用 agent_id 全局映射，
            # 避免同一 agent 的多个会话同时建团队时互相覆盖）
            metadata = getattr(getattr(caller_self, 'config', None), 'metadata', {}) or {}
            tid = metadata.get('team_id')
            if tid and team_manager.get_team(tid):
                return tid
            # Lead/主 agent：会话 -> team_id（团队创建后续请求）
            conv = getattr(caller_self, '_current_conversation', None) or \
                getattr(getattr(caller_self, 'config', None), 'parent_conversation_id', None)
            if conv:
                tid = team_manager.get_team_id_for_session(conv)
                if tid:
                    return tid
        except Exception:
            pass
        # 子 agent: 使用父会话 id（任务归属父会话，跨子 agent 生命周期存活）
        if getattr(caller_self, 'is_subagent', False):
            parent_conv = getattr(caller_self.config, 'parent_conversation_id', None)
            if parent_conv:
                return parent_conv
        # 主 agent: 使用当前会话 id
        conversation = getattr(caller_self, '_current_conversation', None)
        if conversation:
            return conversation
    except Exception:
        pass
    return None


def _resolve_caller_agent_id() -> Optional[str]:
    """解析当前调用者的 agent ID（作为默认 owner）"""
    try:
        agent_id, _, _, _ = _get_parent_agent_info()
        return agent_id
    except Exception:
        return None


async def task_create(
    subject: str,
    description: str = "",
    active_form: str = "",
    task_type: str = "normal",
    blocks: Optional[List[str]] = None,
    blocked_by: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    owner: Optional[str] = None,
) -> Dict[str, Any]:
    """
    创建一个新任务并持久化到磁盘（对应 TaskCreate）。

    任务以独立 JSON 文件存储在 .happy-claw/tasks/{task_list_id}/{task_id}.json，
    可跨会话、跨进程存活。任务 ID 由 1 个类型前缀字母 + 8 个随机字符组成。

    Args:
        subject: 任务简短标题（必填）
        description: 自由格式描述
        active_form: 进行时态描述（任务 in_progress 时在 spinner 显示）
        task_type: 任务类型，决定 ID 前缀:
            normal(普通,前缀n) / shell(本地Shell命令,前缀b) / subagent(本地子代理,前缀a)
            / remote(远程代理,前缀r) / daemon(后台daemon任务,前缀d) / teammate(进程内队友,前缀t)
        blocks: 此任务阻塞的下游任务 ID 列表（会自动同步到下游任务的 blocked_by）
        blocked_by: 阻塞此任务的上游任务 ID 列表（上游未完成时此任务不可被认领）
        metadata: 任意扩展键值对
        owner: 分配的 agent ID（可选，留空表示待认领）
    """
    from app.core.task_system.store import task_store, TASK_TYPE_PREFIXES, VALID_PREFIXES

    if task_type not in TASK_TYPE_PREFIXES and task_type not in VALID_PREFIXES:
        return {
            "success": False,
            "error": f"Invalid task_type: '{task_type}'. Valid: {', '.join(TASK_TYPE_PREFIXES)}",
        }

    task_list_id = _resolve_task_list_id_from_context()
    try:
        task = await task_store.create_task(
            subject=subject,
            description=description,
            active_form=active_form,
            task_type=task_type,
            blocks=blocks or [],
            blocked_by=blocked_by or [],
            metadata=metadata or {},
            owner=owner,
            task_list_id=task_list_id,
        )
        return {"success": True, "task": task}
    except Exception as e:
        logger.error("Task create failed", subject=subject, error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


async def task_get(task_id: str) -> Dict[str, Any]:
    """
    获取指定任务的完整详情（对应 TaskGet）。

    Args:
        task_id: 任务 ID（如 b1a2b3c4d）
    """
    from app.core.task_system.store import task_store

    task_list_id = _resolve_task_list_id_from_context()
    try:
        task = await task_store.get_task(task_id, task_list_id=task_list_id)
        if task is None:
            return {"success": False, "error": f"Task not found: {task_id}"}
        return {"success": True, "task": task}
    except Exception as e:
        logger.error("Task get failed", task_id=task_id, error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


async def task_update(
    task_id: str,
    status: Optional[str] = None,
    owner: Optional[str] = None,
    subject: Optional[str] = None,
    description: Optional[str] = None,
    active_form: Optional[str] = None,
    blocks: Optional[List[str]] = None,
    blocked_by: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    claim: bool = False,
) -> Dict[str, Any]:
    """
    更新任务的 status、blocks、blocked_by 等字段（对应 TaskUpdate）。

    认领语义: 当 claim=true（或 status 设为 in_progress）时，走并发安全的认领流程:
    任务文件锁内依次检查 重新读取(防TOCTOU) → 被他人认领(already_claimed)
    → 已完成(already_resolved) → 上游未完成(blocked) → agent已有其他open任务(agent_busy)，
    全部通过后设置 owner 并置为 in_progress。

    Args:
        task_id: 任务 ID
        status: 新状态 pending/in_progress/completed/deleted
        owner: 认领者 agent ID（认领时缺省为当前调用者）
        subject/description/active_form: 更新对应文本字段（不传则不修改）
        blocks/blocked_by: 整体替换依赖列表（自动双向同步反向边）
        metadata: 合并式更新扩展键值对（不传则不修改）
        claim: 是否执行认领流程（带锁与依赖/忙碌检查）
    """
    from app.core.task_system.store import task_store, TASK_STATUSES

    if status is not None and status not in TASK_STATUSES:
        return {
            "success": False,
            "error": f"Invalid status: '{status}'. Valid: {', '.join(TASK_STATUSES)}",
        }

    task_list_id = _resolve_task_list_id_from_context()

    # 认领路径: 显式 claim 或将状态置为 in_progress 时，均走五步锁检查
    if claim or status == "in_progress":
        claim_owner = owner or _resolve_caller_agent_id()
        if not claim_owner:
            return {"success": False, "error": "Claim requires an owner (agent ID)"}
        try:
            result = await task_store.claim_task(task_id, claim_owner, task_list_id=task_list_id)
        except Exception as e:
            logger.error("Task claim failed", task_id=task_id, error=str(e), exc_info=True)
            return {"success": False, "error": str(e)}
        if not result.get("ok"):
            return {"success": False, **result}
        return {"success": True, "claimed": True, "task": result["task"]}

    # 普通更新路径: 仅包含显式传入的字段（防 TOCTOU，锁内重新读取后合并）
    patch: Dict[str, Any] = {}
    if status is not None:
        patch["status"] = status
    if owner is not None:
        patch["owner"] = owner
    if subject is not None:
        patch["subject"] = subject
    if description is not None:
        patch["description"] = description
    if active_form is not None:
        patch["active_form"] = active_form
    if blocks is not None:
        patch["blocks"] = blocks
    if blocked_by is not None:
        patch["blocked_by"] = blocked_by
    if metadata is not None:
        patch["metadata"] = metadata

    if not patch:
        return {"success": False, "error": "No fields to update"}

    try:
        task, event = await task_store.update_task(task_id, patch, task_list_id=task_list_id)
        if task is None:
            return {"success": False, "error": f"Task not found: {task_id}"}
        return {"success": True, "task": task, "event": event}
    except Exception as e:
        logger.error("Task update failed", task_id=task_id, error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


async def task_list(
    status: Optional[str] = None,
    owner: Optional[str] = None,
) -> Dict[str, Any]:
    """
    列出当前任务列表中的所有任务（对应 TaskList），可按状态/所有者过滤。
    用于查询任务归属（ownership）与依赖状态，避免重复工作。

    Args:
        status: 按状态过滤（可选）: pending/in_progress/completed/deleted
        owner: 按所有者 agent ID 过滤（可选）
    """
    from app.core.task_system.store import task_store

    task_list_id = _resolve_task_list_id_from_context()
    try:
        tasks = await task_store.list_tasks(status=status, owner=owner, task_list_id=task_list_id)
        summaries = [
            {
                "id": t.get("id"),
                "subject": t.get("subject"),
                "status": t.get("status"),
                "owner": t.get("owner"),
                "active_form": t.get("active_form"),
                "blocks": t.get("blocks", []),
                "blocked_by": t.get("blocked_by", []),
            }
            for t in tasks
        ]
        return {
            "success": True,
            "task_list_id": task_list_id or task_store.resolve_task_list_id(None),
            "total": len(tasks),
            "tasks": summaries,
            "details": tasks,
        }
    except Exception as e:
        logger.error("Task list failed", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


# ===== 定时任务工具（CronCreate/CronDelete/CronList） =====

def _resolve_schedule_session_id() -> Optional[str]:
    """解析当前调用上下文的会话 ID（定时任务归属）

    子 agent 的会话是临时的，定时任务应归属父会话，避免子 agent 退出后任务成为孤儿。
    """
    try:
        _, _, _, caller_self = _get_parent_agent_info()
        if getattr(caller_self, 'is_subagent', False):
            parent_conv = getattr(caller_self.config, 'parent_conversation_id', None)
            if parent_conv:
                return parent_conv
        conversation = getattr(caller_self, '_current_conversation', None)
        if conversation:
            return conversation
    except Exception:
        pass
    return None


async def cron_create(
    schedule: str = "",
    prompt: str = "",
    name: str = "",
    description: str = "",
    recurring: bool = True,
    durable: bool = True,
    at: str = "",
) -> Dict[str, Any]:
    """
    创建一个定时任务（CronCreate）。任务触发时会自动启动一个 agent 按指令完成任务，
    并将结果注入到当前会话中。

    任务存储在 .happy-claw/scheduled-tasks/{session-id}/{task-id}/ 目录，包含:
    - SKILL.md: 触发时 agent 执行的指令提示（由 prompt 参数生成）
    - task.json: 任务元数据（cron 表达式、recurring、durable 等）
    - .output: 最近一次执行结果（重复任务覆盖写）

    Args:
        schedule: 标准 5 字段 cron 表达式（分 时 日 月 星期），支持 *、*/N、N、N-M、N-M/S、N,M,...
                  不支持 L、W、?。例如 "0 9 * * *" 表示每天 9 点，"* * * * *" 表示每分钟。
                  与 at 二选一。
        prompt: 任务触发时需要执行的指令内容（将写入 SKILL.md 正文），应尽量具体可执行。
        name: 任务名称（可选，用于展示）
        description: 任务描述（可选，如"每天7点自动生成简报"）
        recurring: 是否重复执行（默认 true）。false 时为一次性任务，执行后自动删除。
        durable: 是否持久化（默认 true）。false 的任务在应用重启后会被清理。
        at: 一次性任务的执行时间（ISO 格式，如 "2026-08-30T14:00:00"），与 schedule 二选一。
    """
    from app.core.scheduler.store import schedule_store, MaxScheduleExceededError
    from app.core.scheduler.scheduler import compute_next_fire_ms
    from app.core.scheduler.cron import parse as parse_cron, CronParseError
    from datetime import datetime as _dt

    if not prompt or not prompt.strip():
        return {"success": False, "error": "prompt is required (the instructions for the scheduled task)"}

    # 解析归属会话与 agent
    session_id = _resolve_schedule_session_id()
    if not session_id:
        return {"success": False, "error": "Cannot determine current conversation for scheduled task"}
    agent_id = _resolve_caller_agent_id() or ""

    # 参数校验：schedule 与 at 二选一
    if schedule and at:
        return {"success": False, "error": "Provide either 'schedule' (cron) or 'at' (one-time ISO datetime), not both"}
    if not schedule and not at:
        return {"success": False, "error": "Either 'schedule' (cron expression) or 'at' (one-time datetime) is required"}

    if schedule:
        try:
            parse_cron(schedule)
        except CronParseError as e:
            return {"success": False, "error": f"Invalid cron expression: {str(e)}"}
        # 有明确 cron 表达式且未显式指定 recurring 时，按表达式语义默认重复执行（保留入参）
    if at:
        try:
            _dt.fromisoformat(at)
        except Exception:
            return {"success": False, "error": f"Invalid 'at' datetime: '{at}' (expected ISO format, e.g. 2026-08-30T14:00:00)"}
        recurring = False

    # 生成 SKILL.md（带 frontmatter）
    skill_name = name or f"scheduled-task"
    skill_md = (
        f"---\n"
        f"name: {skill_name}\n"
        f"description: {description or name or 'Scheduled task'}\n"
        f"mode: bypassPermissions\n"
        f"---\n"
        f"{prompt.strip()}\n"
    )

    try:
        state = schedule_store.create(
            session_id=session_id,
            agent_id=agent_id,
            skill_md=skill_md,
            cron=schedule or None,
            at=at or None,
            recurring=recurring,
            durable=durable,
            name=name,
            description=description,
        )
    except MaxScheduleExceededError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error("Cron create failed", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}

    # 计算首次触发时间（含抖动）
    next_fire_at = compute_next_fire_ms(state)
    if next_fire_at is None:
        schedule_store.delete(session_id, state["id"])
        return {"success": False, "error": "Schedule has no future fire time (already expired?)"}
    schedule_store.update_state(session_id, state["id"], {"next_fire_at": next_fire_at})

    next_fire_iso = _dt.fromtimestamp(next_fire_at / 1000).isoformat(timespec="seconds")
    logger.info("Cron task created", task_id=state["id"], session_id=session_id, cron=schedule, at=at)

    return {
        "success": True,
        "task": {
            "id": state["id"],
            "cron": schedule or None,
            "at": at or None,
            "recurring": recurring,
            "durable": durable,
            "name": name,
            "next_fire_at": next_fire_iso,
        },
        "message": f"Scheduled task created. Next run: {next_fire_iso}",
    }


async def cron_delete(task_id: str) -> Dict[str, Any]:
    """
    删除（取消）一个定时任务（CronDelete）。

    Args:
        task_id: 定时任务 ID（由 cron_create 返回，如 abc12345）
    """
    from app.core.scheduler.store import schedule_store

    session_id = _resolve_schedule_session_id()

    # 优先在当前会话中查找，找不到则全局查找（允许跨会话取消）
    target_session = None
    if session_id and schedule_store.get(session_id, task_id):
        target_session = session_id
    else:
        for t in schedule_store.list_tasks():
            if t.get("id") == task_id:
                target_session = t.get("session_id")
                break

    if not target_session:
        return {"success": False, "error": f"Scheduled task not found: {task_id}"}

    schedule_store.delete(target_session, task_id)
    return {"success": True, "message": f"Scheduled task '{task_id}' cancelled"}


async def cron_list() -> Dict[str, Any]:
    """
    列出定时任务（CronList）。优先列出当前会话的任务，同时返回全局任务总数。
    可用于查询任务 ID 以便取消任务。
    """
    from app.core.scheduler.store import schedule_store
    from datetime import datetime as _dt

    session_id = _resolve_schedule_session_id()
    all_tasks = schedule_store.list_tasks()
    session_tasks = [t for t in all_tasks if t.get("session_id") == session_id] if session_id else []

    def _fmt(t: Dict[str, Any]) -> Dict[str, Any]:
        next_ms = t.get("next_fire_at")
        last_ms = t.get("last_run_at")
        return {
            "id": t.get("id"),
            "name": t.get("name") or "",
            "description": t.get("description") or "",
            "cron": t.get("cron"),
            "at": t.get("at"),
            "recurring": t.get("recurring"),
            "durable": t.get("durable"),
            "next_fire_at": _dt.fromtimestamp(next_ms / 1000).isoformat(timespec="seconds") if next_ms else None,
            "last_run_at": _dt.fromtimestamp(last_ms / 1000).isoformat(timespec="seconds") if last_ms else None,
            "session_id": t.get("session_id"),
            "in_current_session": t.get("session_id") == session_id,
        }

    return {
        "success": True,
        "total": len(all_tasks),
        "current_session_count": len(session_tasks),
        "tasks": [_fmt(t) for t in all_tasks],
    }


# ===== 注册工具到注册表 =====

def register_builtin_tools():
    """注册所有内置工具"""
    
    # Web Search 工具
    tool_registry.register(
        name="web_search",
        description="Execute a web search to find information on the internet. Supports multiple providers (brave, duckduckgo, toutiao, serpapi, google). Default provider is Brave (free). Use this when you need current information, news, or real-time data.",
        handler=web_search,
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query or keywords",
                },
                "num_results": {
                    "type": "integer",
                    "description": "Number of results to return (1-10)",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 5,
                },
            },
            "required": ["query"],
        },
        required_role=ToolPermissionEnum.PUBLIC,
        timeout_seconds=10,
    )
    
    # Bash Command 工具
    system_info = f"""
Environment Information:
- Operating System: {platform.system()} {platform.version()}
- Platform: {platform.platform()}
- Python Version: {platform.python_version()}
- Current Directory: {os.getcwd()}
- Default Shell: {_detect_shell()}

Command Syntax Guidelines:
- Windows: Use PowerShell commands (e.g., Get-ChildItem, Select-String, Where-Object)
- Linux/macOS: Use Bash commands (e.g., ls, grep, find, awk, sed)

Examples:
- Windows PowerShell: "Get-ChildItem -Path . -Recurse -Filter '*.py'"
- Linux Bash: "find . -name '*.py' -type f"
"""
    
    tool_registry.register(
        name="bash_command",
        description=f"Execute a shell command in the system terminal. {system_info}\n\n️ SECURITY WARNING: This tool can execute arbitrary system commands. Use with caution!",
        handler=bash_command,
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": f"The shell command to execute. Use {_detect_shell()} syntax based on your OS.",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Command timeout in seconds",
                    "minimum": 1,
                    "maximum": 3600,
                    "default": 30,
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": "Set to true to run the command in the background. Use this for long-running commands like pip install, npm install, pytest, dev servers, etc. The result will be injected into the conversation when complete.",
                    "default": False,
                },
            },
            "required": ["command"],
        },
        required_role=ToolPermissionEnum.ADMIN,  # 需要管理员权限
        timeout_seconds=60,
    )
    
    # Read File 工具
    tool_registry.register(
        name="read_file",
        description="Read the contents of a file from the filesystem. Useful for examining code, configuration files, logs, etc.",
        handler=read_file,
        parameters={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file (relative or absolute)",
                },
                "max_lines": {
                    "type": "integer",
                    "description": "Maximum number of lines to read (to prevent reading very large files)",
                    "minimum": 1,
                    "maximum": 10000,
                    "default": 1000,
                },
            },
            "required": ["file_path"],
        },
        required_role=ToolPermissionEnum.PUBLIC,
        timeout_seconds=5,
    )
    
    # Write File 工具
    tool_registry.register(
        name="write_file",
        description="Write content to a file. Can create new files or overwrite/append to existing files.",
        handler=write_file,
        parameters={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file (relative or absolute)",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write to the file",
                },
                "append": {
                    "type": "boolean",
                    "description": "Whether to append to existing file (true) or overwrite (false)",
                    "default": False,
                },
            },
            "required": ["file_path", "content"],
        },
        required_role=ToolPermissionEnum.ADMIN,  # 需要管理员权限
        timeout_seconds=5,
    )

    # Edit File 工具（精确字符串替换）
    tool_registry.register(
        name="edit_file",
        description="Make precise string replacements in an existing file. "
                    "Requires old_string to uniquely match in the file (to avoid hallucinations). "
                    "Set replace_all=true to replace all occurrences.",
        handler=edit_file,
        parameters={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file to edit (relative or absolute)",
                },
                "old_string": {
                    "type": "string",
                    "description": "The exact string to find and replace. Must be unique in the file.",
                },
                "new_string": {
                    "type": "string",
                    "description": "The new string to replace with",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences instead of requiring unique match (default false)",
                    "default": False,
                },
            },
            "required": ["file_path", "old_string", "new_string"],
        },
        required_role=ToolPermissionEnum.ADMIN,
        timeout_seconds=10,
    )

    # Glob 文件搜索工具
    tool_registry.register(
        name="glob",
        description="Find files by name or path using glob patterns (e.g. **/*.ts, src/*.py). "
                    "Results are sorted by modification time (most recent first).",
        handler=glob_search,
        parameters={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to match files (e.g. '**/*.py', 'src/**/*.ts')",
                },
                "path": {
                    "type": "string",
                    "description": "Root directory to search in (default '.')",
                    "default": ".",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default 100)",
                    "default": 100,
                },
            },
            "required": ["pattern"],
        },
        required_role=ToolPermissionEnum.PUBLIC,
        timeout_seconds=10,
    )

    # Grep 内容搜索工具
    tool_registry.register(
        name="grep",
        description="Search file contents using regular expressions. Powered by ripgrep (rg) for extreme speed. "
                    "Supports output modes: 'files_with_matches' (list matching files only) "
                    "or 'content' (show matching lines with optional context).",
        handler=grep_search,
        parameters={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regular expression pattern to search for",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search in (default '.')",
                    "default": ".",
                },
                "include": {
                    "type": "string",
                    "description": "Glob pattern to filter files (e.g. '*.py', '*.ts')",
                    "default": "",
                },
                "case_sensitive": {
                    "type": "boolean",
                    "description": "Whether the search is case-sensitive (default true)",
                    "default": True,
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results (default 100)",
                    "default": 100,
                },
                "context_lines": {
                    "type": "integer",
                    "description": "Number of context lines to show before/after each match (default 0)",
                    "default": 0,
                },
                "output_mode": {
                    "type": "string",
                    "description": "Output mode: 'content' for matching lines, 'files_with_matches' for file list only",
                    "enum": ["content", "files_with_matches"],
                    "default": "content",
                },
            },
            "required": ["pattern"],
        },
        required_role=ToolPermissionEnum.PUBLIC,
        timeout_seconds=30,
    )

    # Todo Write 工具（系统工具，不显示在前端，不保存到数据库）
    tool_registry.register(
        name="todo_write",
        description="Create and manage a task list for your current task session. Use this to track progress when working on complex multi-step tasks.",
        handler=todo_write,
        parameters={
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "List of tasks with content and status",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "Task description",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                                "description": "Task status",
                            },
                        },
                        "required": ["content", "status"],
                    },
                },
            },
            "required": ["todos"],
        },
        required_role=ToolPermissionEnum.PUBLIC,
        timeout_seconds=5,
        metadata={"system_tool": True},  # 系统工具：不显示在前端
    )
    
    # Spawn Agent 工具（显示在前端，带 DAG 可视化）
    tool_registry.register(
        name="spawn_agent",
        description="Spawn subagents to handle tasks. Supports single or multiple agents with dependency management. Independent agents run concurrently. Agents with dependencies wait for their dependencies to complete first.",
        handler=spawn_agent,
        parameters={
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "List of tasks to spawn as subagents. Each task can specify dependencies on other tasks by name.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "subagent_name": {
                                "type": "string",
                                "description": "Name of the subagent (used for dependency references)",
                            },
                            "subagent_role": {
                                "type": "string",
                                "description": "Role description defining behavior pattern and expertise area (not required for skill type)",
                            },
                            "task_description": {
                                "type": "string",
                                "description": "Specific task description to assign to the subagent",
                            },
                            "agent_type": {
                                "type": "string",
                                "description": "Agent type: 'common' (default, normal assignment) or 'skill' (skill-based assignment, subagent_name must be a skill name, subagent_role not needed)",
                                "enum": ["common", "skill"],
                                "default": "common",
                            },
                            "depends_on": {
                                "type": "array",
                                "description": "List of subagent names this task depends on (optional)",
                                "items": {"type": "string"},
                                "default": [],
                            },
                            "allowed_tools": {
                                "type": "array",
                                "description": "Tools allowed for this subagent (optional)",
                                "items": {"type": "string"},
                            },
                            "config_overrides": {
                                "type": "object",
                                "description": "Configuration overrides (optional), can specify llm_model, temperature, etc.",
                                "properties": {
                                    "llm_model": {"type": "string"},
                                    "temperature": {"type": "number", "minimum": 0, "maximum": 2},
                                    "max_tokens": {"type": "integer", "minimum": 1},
                                },
                            },
                        },
                        "required": ["subagent_name", "task_description"],
                    },
                },
            },
            "required": ["tasks"],
        },
        required_role=ToolPermissionEnum.ADMIN,
        timeout_seconds=300,
        metadata={"system_tool": False},  # 显示在前端，带 DAG 可视化
    )
    
    # Load Skill 工具（系统工具，不显示在前端）
    tool_registry.register(
        name="load_skill",
        description="Load a skill's full instructions by name. Skills provide extensible instruction sets for specific tasks. Only call this when you need to execute a specific skill.",
        handler=load_skill,
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The exact name of the skill to load (must match a registered skill name)",
                },
                "args": {
                    "type": "object",
                    "description": "Optional arguments to pass to the skill (format depends on the skill's parameter definition)",
                },
            },
            "required": ["name"],
        },
        required_role=ToolPermissionEnum.PUBLIC,
        timeout_seconds=5,
        metadata={"system_tool": True},  # 系统工具：不显示在前端
    )

    # Task Create 工具（任务系统）
    tool_registry.register(
        name="task_create",
        description="Create a new persistent task (TaskCreate). Tasks are stored as JSON files on disk and survive across sessions and processes. Supports dependency declarations (blocks/blocked_by) and ownership assignment.",
        handler=task_create,
        parameters={
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "description": "Short title of the task",
                },
                "description": {
                    "type": "string",
                    "description": "Free-form description of the task",
                },
                "active_form": {
                    "type": "string",
                    "description": "Present-continuous form shown in spinner while in_progress (e.g. 'Analyzing data...')",
                },
                "task_type": {
                    "type": "string",
                    "description": "Task type determining ID prefix: normal(n)/shell(b)/subagent(a)/remote(r)/daemon(d)/teammate(t)",
                    "enum": ["normal", "shell", "subagent", "remote", "daemon", "teammate"],
                    "default": "normal",
                },
                "blocks": {
                    "type": "array",
                    "description": "Task IDs that this task blocks (downstream)",
                    "items": {"type": "string"},
                },
                "blocked_by": {
                    "type": "array",
                    "description": "Task IDs that block this task (upstream). This task cannot be claimed until they complete.",
                    "items": {"type": "string"},
                },
                "metadata": {
                    "type": "object",
                    "description": "Arbitrary extension key-value pairs",
                },
                "owner": {
                    "type": "string",
                    "description": "Agent ID assigned to the task (optional, leave empty for unclaimed)",
                },
            },
            "required": ["subject"],
        },
        required_role=ToolPermissionEnum.PUBLIC,
        timeout_seconds=10,
        metadata={"system_tool": False},
    )

    # Task Get 工具（任务系统）
    tool_registry.register(
        name="task_get",
        description="Get full details of a task by its ID (TaskGet).",
        handler=task_get,
        parameters={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task ID (e.g. b1a2b3c4d)",
                },
            },
            "required": ["task_id"],
        },
        required_role=ToolPermissionEnum.PUBLIC,
        timeout_seconds=10,
        metadata={"system_tool": False},
    )

    # Task Update 工具（任务系统）
    tool_registry.register(
        name="task_update",
        description="Update a task's status, blocks, blocked_by and other fields (TaskUpdate). With claim=true (or status=in_progress) it performs a concurrency-safe claim: file-locked checks for already_claimed / already_resolved / blocked / agent_busy before setting owner and in_progress.",
        handler=task_update,
        parameters={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task ID to update",
                },
                "status": {
                    "type": "string",
                    "description": "New lifecycle status",
                    "enum": ["pending", "in_progress", "completed", "deleted"],
                },
                "owner": {
                    "type": "string",
                    "description": "Owner agent ID (defaults to current agent when claiming)",
                },
                "subject": {
                    "type": "string",
                    "description": "New short title (optional)",
                },
                "description": {
                    "type": "string",
                    "description": "New description (optional)",
                },
                "active_form": {
                    "type": "string",
                    "description": "New active form (optional)",
                },
                "blocks": {
                    "type": "array",
                    "description": "Replace the downstream dependency list (auto-syncs reverse edges)",
                    "items": {"type": "string"},
                },
                "blocked_by": {
                    "type": "array",
                    "description": "Replace the upstream dependency list (auto-syncs reverse edges)",
                    "items": {"type": "string"},
                },
                "metadata": {
                    "type": "object",
                    "description": "Merge-update extension key-value pairs",
                },
                "claim": {
                    "type": "boolean",
                    "description": "Whether to perform the locked claim flow (dependency + busy checks)",
                    "default": False,
                },
            },
            "required": ["task_id"],
        },
        required_role=ToolPermissionEnum.PUBLIC,
        timeout_seconds=10,
        metadata={"system_tool": False},
    )

    # Task List 工具（任务系统）
    tool_registry.register(
        name="task_list",
        description="List all tasks in the current task list (TaskList). Supports filtering by status or owner. Use this to query task ownership and dependency state to avoid duplicate work.",
        handler=task_list,
        parameters={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Filter by status (optional)",
                    "enum": ["pending", "in_progress", "completed", "deleted"],
                },
                "owner": {
                    "type": "string",
                    "description": "Filter by owner agent ID (optional)",
                },
            },
        },
        required_role=ToolPermissionEnum.PUBLIC,
        timeout_seconds=10,
        metadata={"system_tool": False},
    )

    # Cron Create 工具（定时任务）
    tool_registry.register(
        name="cron_create",
        description="Create a scheduled task (CronCreate). The task runs automatically on schedule: a dedicated agent executes the given instructions (prompt) and the result is delivered back into this conversation. Use a standard 5-field cron expression (minute hour day month weekday) in 'schedule', or an ISO datetime in 'at' for a one-time task. One-time tasks are deleted after execution. Max tasks limit applies globally.",
        handler=cron_create,
        parameters={
            "type": "object",
            "properties": {
                "schedule": {
                    "type": "string",
                    "description": "Standard 5-field cron expression (minute hour day month weekday). Supports *、*/N、N、N-M、N-M/S、N,M,... (L/W/? not supported). E.g. '0 9 * * *' = daily at 9:00, '*/5 * * * *' = every 5 minutes.",
                },
                "prompt": {
                    "type": "string",
                    "description": "The instructions the agent should execute when the task fires (written to SKILL.md). Make it concrete and actionable.",
                },
                "name": {
                    "type": "string",
                    "description": "Optional task name (e.g. 'morning-briefing')",
                },
                "description": {
                    "type": "string",
                    "description": "Optional description (e.g. '每天7点自动生成简报')",
                },
                "recurring": {
                    "type": "boolean",
                    "description": "Whether the task repeats (default true). false = one-time task, deleted after execution.",
                    "default": True,
                },
                "durable": {
                    "type": "boolean",
                    "description": "Whether the task survives app restarts (default true).",
                    "default": True,
                },
                "at": {
                    "type": "string",
                    "description": "One-time execution datetime in ISO format (e.g. '2026-08-30T14:00:00'). Mutually exclusive with 'schedule'.",
                },
            },
            "required": ["prompt"],
        },
        required_role=ToolPermissionEnum.PUBLIC,
        timeout_seconds=10,
        metadata={"system_tool": False},
    )

    # Cron Delete 工具（定时任务）
    tool_registry.register(
        name="cron_delete",
        description="Cancel and delete a scheduled task by its ID (CronDelete). Use cron_list to find task IDs.",
        handler=cron_delete,
        parameters={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The scheduled task ID to cancel (e.g. abc12345)",
                },
            },
            "required": ["task_id"],
        },
        required_role=ToolPermissionEnum.PUBLIC,
        timeout_seconds=10,
        metadata={"system_tool": False},
    )

    # Cron List 工具（定时任务）
    tool_registry.register(
        name="cron_list",
        description="List all scheduled tasks (CronList), including cron expression, next/last fire times and whether they belong to the current conversation. Use this to find task IDs for cancellation.",
        handler=cron_list,
        parameters={
            "type": "object",
            "properties": {},
        },
        required_role=ToolPermissionEnum.PUBLIC,
        timeout_seconds=10,
        metadata={"system_tool": False},
    )

    # ===== 团队协作工具（仅在 ENABLE_AGENT_TEAMS 开启时注册） =====
    try:
        from app.tools.team_tools import register_team_tools
        register_team_tools()
    except Exception as _e:
        logger.warning("Failed to register team tools", error=str(_e))

    # ===== 图片生成工具（仅在有 agent 配置 mediaModels 时可用） =====
    try:
        from app.tools.image_generate import register_image_generate_tool
        register_image_generate_tool()
    except Exception as _e:
        logger.warning("Failed to register image_generate tool", error=str(_e))

    # ===== 音乐生成工具（仅在有 agent 配置 mediaModels.music 时可用） =====
    try:
        from app.tools.music_generate import register_music_generate_tool
        register_music_generate_tool()
    except Exception as _e:
        logger.warning("Failed to register music_generate tool", error=str(_e))

    # ===== 视频生成工具（仅在有 agent 配置 mediaModels.video 时可用） =====
    try:
        from app.tools.video_generate import register_video_generate_tool
        register_video_generate_tool()
    except Exception as _e:
        logger.warning("Failed to register video_generate tool", error=str(_e))

    # ===== PDF 解析工具（仅在有 agent 配置 pdf 时可用） =====
    try:
        from app.tools.pdf_parse import register_pdf_parse_tool
        register_pdf_parse_tool()
    except Exception as _e:
        logger.warning("Failed to register pdf_parse tool", error=str(_e))

    # 注册团队权限门控钩子（幂等，只在首次注册）
    try:
        from app.core.team_system.manager import register_team_hooks
        register_team_hooks()
    except Exception as _e:
        logger.warning("Failed to register team hooks", error=str(_e))

    return len(tool_registry.list_tools())


# 自动注册（模块导入时执行）
register_builtin_tools()
