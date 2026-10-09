"""web_search 工具单元测试"""
import sys
from pathlib import Path

# 直接运行时确保 app 模块可导入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import httpx
from unittest.mock import patch, AsyncMock, MagicMock

from app.tools.builtin_tools import (
    web_search,
    _search_brave,
    _search_duckduckgo,
    _search_google,
    _search_serpapi,
    _search_playwright,
    _get_env,
    _load_web_search_config,
    _SEARCH_PROVIDERS,
)

from app.config import settings


# ===== _get_env 测试 =====

class TestGetEnv:
    """测试 _get_env 环境变量读取"""

    def test_read_from_environ(self):
        """从 os.environ 读取"""
        with patch.dict("os.environ", {"TEST_KEY_ABC": "hello"}):
            assert _get_env("TEST_KEY_ABC") == "hello"

    def test_return_default_when_missing(self):
        """不存在时返回默认值"""
        assert _get_env("NONEXISTENT_KEY_XYZ", default="fallback") == "fallback"

    def test_read_from_env_file(self, tmp_path):
        """从 .env 文件读取"""
        env_file = tmp_path / ".env"
        env_file.write_text("MY_SECRET=secret_value\n", encoding="utf-8")
        with patch("app.tools.builtin_tools.Path") as mock_path:
            mock_path.return_value.resolve.return_value.parent.parent.parent = tmp_path
            val = _get_env("MY_SECRET")
            # 可能读取到也可能读不到，取决于路径拼接，这里只验证不报错
            assert isinstance(val, str)


# ===== _load_web_search_config 测试 =====

class TestLoadWebSearchConfig:
    """测试配置加载"""

    def test_return_dict_when_no_config(self):
        """无配置时返回空字典"""
        mock_settings = MagicMock()
        mock_settings.tools.web_search = None
        with patch("app.tools.builtin_tools.settings", mock_settings):
            result = _load_web_search_config()
            assert isinstance(result, dict)

    def test_return_config_from_settings(self):
        """从 settings 读取配置"""
        mock_settings = MagicMock()
        mock_settings.tools.web_search = {"provider": "duckduckgo"}
        with patch("app.tools.builtin_tools.settings", mock_settings):
            result = _load_web_search_config()
            assert result.get("provider") == "duckduckgo"

    def test_return_dict_on_exception(self):
        """异常时返回空字典"""
        with patch("app.tools.builtin_tools.settings", side_effect=RuntimeError("fail")):
            result = _load_web_search_config()
            assert isinstance(result, dict)


# ===== _search_brave 测试 =====

class TestSearchBrave:
    """测试 Brave Search"""

    @pytest.mark.asyncio
    async def test_missing_api_key(self):
        """未配置 API key 时返回错误"""
        with patch("app.tools.builtin_tools._get_env", return_value=""):
            result = await _search_brave("test", 5, {})
            assert result["success"] is False
            assert "not configured" in result["error"]

    @pytest.mark.asyncio
    async def test_successful_search(self):
        """模拟成功搜索"""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "web": {
                "results": [
                    {"title": "Result 1", "url": "https://example.com/1", "description": "Desc 1"},
                    {"title": "Result 2", "url": "https://example.com/2", "description": "Desc 2"},
                ]
            }
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("app.tools.builtin_tools._get_env", return_value="fake-key"):
            with patch("app.tools.builtin_tools.httpx.AsyncClient", return_value=mock_client):
                result = await _search_brave("python", 5, {})
                assert result["success"] is True
                assert result["provider"] == "brave"
                assert result["results_count"] == 2
                assert result["results"][0]["title"] == "Result 1"


# ===== _search_duckduckgo 测试 =====

class TestSearchDuckduckgo:
    """测试 DuckDuckGo Search"""

    @pytest.mark.asyncio
    async def test_successful_search_with_abstract(self):
        """有摘要结果的搜索"""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "Abstract": "Python is a programming language",
            "Heading": "Python",
            "AbstractURL": "https://en.wikipedia.org/wiki/Python",
            "RelatedTopics": [],
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("app.tools.builtin_tools.httpx.AsyncClient", return_value=mock_client):
            result = await _search_duckduckgo("python", 5, {})
            assert result["success"] is True
            assert result["provider"] == "duckduckgo"
            assert result["results_count"] >= 1
            assert result["results"][0]["title"] == "Python"

    @pytest.mark.asyncio
    async def test_empty_results(self):
        """无结果时返回提示"""
        mock_response = MagicMock()
        mock_response.json.return_value = {"Abstract": None, "RelatedTopics": []}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("app.tools.builtin_tools.httpx.AsyncClient", return_value=mock_client):
            result = await _search_duckduckgo("xyznonexistent", 5, {})
            assert result["success"] is True
            assert result["results_count"] == 0
            assert "message" in result

    @pytest.mark.asyncio
    async def test_related_topics(self):
        """相关主题结果"""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "Abstract": None,
            "RelatedTopics": [
                {"Text": "Topic A description", "FirstURL": "https://example.com/a"},
                {"Text": "Topic B description", "FirstURL": "https://example.com/b"},
            ],
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("app.tools.builtin_tools.httpx.AsyncClient", return_value=mock_client):
            result = await _search_duckduckgo("test", 5, {})
            assert result["success"] is True
            assert result["results_count"] == 2


# ===== _search_google 测试 =====

class TestSearchGoogle:
    """测试 Google Custom Search"""

    @pytest.mark.asyncio
    async def test_missing_api_key(self):
        """未配置 API key 时返回错误"""
        with patch("app.tools.builtin_tools._get_env", return_value=""):
            result = await _search_google("test", 5, {})
            assert result["success"] is False
            assert "not configured" in result["error"]

    @pytest.mark.asyncio
    async def test_successful_search(self):
        """模拟成功搜索"""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "items": [
                {"title": "Google Result", "link": "https://example.com", "snippet": "Snippet"},
            ]
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("app.tools.builtin_tools._get_env", return_value="fake-key"):
            with patch("app.tools.builtin_tools.httpx.AsyncClient", return_value=mock_client):
                result = await _search_google("test", 5, {})
                assert result["success"] is True
                assert result["provider"] == "google"
                assert result["results_count"] == 1


# ===== _search_serpapi 测试 =====

class TestSearchSerpapi:
    """测试 SerpAPI Search"""

    @pytest.mark.asyncio
    async def test_missing_api_key(self):
        """未配置 API key 时返回错误"""
        with patch("app.tools.builtin_tools._get_env", return_value=""):
            result = await _search_serpapi("test", 5, {})
            assert result["success"] is False
            assert "not configured" in result["error"]


# ===== web_search 主函数测试 =====

class TestWebSearch:
    """测试 web_search 主入口函数"""

    @pytest.mark.asyncio
    async def test_num_results_clamp_min(self):
        """num_results 下限限制为 1"""
        with patch("app.tools.builtin_tools._load_web_search_config", return_value={}):
            with patch("app.tools.builtin_tools._SEARCH_PROVIDERS", {"brave": AsyncMock(return_value={"success": True, "results": [], "results_count": 0})}):
                result = await web_search("test", num_results=0)
                # 不应报错，num_results 被 clamp 到 1
                assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_num_results_clamp_max(self):
        """num_results 上限限制为 10"""
        with patch("app.tools.builtin_tools._load_web_search_config", return_value={}):
            with patch("app.tools.builtin_tools._SEARCH_PROVIDERS", {"brave": AsyncMock(return_value={"success": True, "results": [], "results_count": 0})}):
                result = await web_search("test", num_results=20)
                assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_unknown_provider(self):
        """未知提供商返回错误"""
        config = {"provider": "nonexistent_provider"}
        with patch("app.tools.builtin_tools._load_web_search_config", return_value=config):
            result = await web_search("test", num_results=5)
            assert result["success"] is False
            assert "Unknown search provider" in result["error"]

    @pytest.mark.asyncio
    async def test_provider_routing(self):
        """正确路由到配置的提供商"""
        mock_handler = AsyncMock(return_value={
            "success": True, "provider": "duckduckgo", "results": [], "results_count": 0
        })
        config = {"provider": "duckduckgo", "providers": {}}
        with patch("app.tools.builtin_tools._load_web_search_config", return_value=config):
            with patch("app.tools.builtin_tools._SEARCH_PROVIDERS", {"duckduckgo": mock_handler}):
                result = await web_search("test query", num_results=3)
                assert result["success"] is True
                mock_handler.assert_called_once_with("test query", 3, {})

    @pytest.mark.asyncio
    async def test_http_error_handling(self):
        """HTTP 错误处理"""
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_handler = AsyncMock(side_effect=httpx.HTTPStatusError(
            "Forbidden", request=MagicMock(), response=mock_response
        ))
        config = {"provider": "brave", "providers": {}}
        with patch("app.tools.builtin_tools._load_web_search_config", return_value=config):
            with patch("app.tools.builtin_tools._SEARCH_PROVIDERS", {"brave": mock_handler}):
                result = await web_search("test", num_results=5)
                assert result["success"] is False
                assert "403" in result["error"]

    @pytest.mark.asyncio
    async def test_request_error_handling(self):
        """网络请求错误处理"""
        mock_handler = AsyncMock(side_effect=httpx.RequestError("Connection refused"))
        config = {"provider": "brave", "providers": {}}
        with patch("app.tools.builtin_tools._load_web_search_config", return_value=config):
            with patch("app.tools.builtin_tools._SEARCH_PROVIDERS", {"brave": mock_handler}):
                result = await web_search("test", num_results=5)
                assert result["success"] is False
                assert "Connection refused" in result["error"]

    @pytest.mark.asyncio
    async def test_generic_exception_handling(self):
        """未知异常处理"""
        mock_handler = AsyncMock(side_effect=RuntimeError("Something went wrong"))
        config = {"provider": "brave", "providers": {}}
        with patch("app.tools.builtin_tools._load_web_search_config", return_value=config):
            with patch("app.tools.builtin_tools._SEARCH_PROVIDERS", {"brave": mock_handler}):
                result = await web_search("test", num_results=5)
                assert result["success"] is False
                assert "Something went wrong" in result["error"]

    @pytest.mark.asyncio
    async def test_default_provider_is_brave(self):
        """默认使用 brave 提供商"""
        mock_handler = AsyncMock(return_value={
            "success": True, "provider": "brave", "results": [], "results_count": 0
        })
        # 空配置 → 默认 brave
        with patch("app.tools.builtin_tools._load_web_search_config", return_value={}):
            with patch("app.tools.builtin_tools._SEARCH_PROVIDERS", {"brave": mock_handler}):
                result = await web_search("test", num_results=5)
                mock_handler.assert_called_once()
                assert result["success"] is True

    @pytest.mark.asyncio
    async def test_search(self):
        """搜索测试"""
        result = await web_search("阿里AI", num_results=5)
        print("result:", result)
        test = 1


# ===== _SEARCH_PROVIDERS 映射测试 =====

class TestSearchProvidersMapping:
    """测试提供商映射完整性"""

    def test_all_providers_registered(self):
        """所有预期提供商已注册"""
        expected = {"brave", "serpapi", "google", "duckduckgo", "toutiao", "playwright"}
        assert set(_SEARCH_PROVIDERS.keys()) == expected

    def test_all_handlers_are_callable(self):
        """所有处理器都是可调用对象"""
        for name, fn in _SEARCH_PROVIDERS.items():
            assert callable(fn), f"{name} handler is not callable"


class TestSearchProviders:
    """测试提供商功能"""

    @pytest.mark.asyncio
    async def test_search(self):
        """搜索测试"""
        result = await web_search("内存条", num_results=5)
        print("result:", result)
        test = 1


if __name__ == "__main__":
    # 运行 TestLogin 类下的 test_login_success 方法
    pytest.main(["-k", "TestSearchProviders and test_search"])
    # pytest.main(["-k", "TestWebSearch or TestSearchProvidersMapping"])
