"""
oMLX API 客户端
封装 OpenAI 兼容的 API 调用，同时提供同步与异步两种方式。

说明：
- 同步方式使用 OpenAI SDK 的同步客户端（OpenAI）
- 异步方式使用 OpenAI SDK 的异步客户端（AsyncOpenAI）
  该异步客户端已内部处理 httpx 流式响应的清理问题，
  规避了 Python 3.13 + httpx 下的
  "generator didn't stop after athrow()" 兼容性故障（详见 docs/async-stream-issue.md）
"""

from typing import List, Optional, Dict, Any, Generator, AsyncGenerator

from openai import AsyncOpenAI, OpenAI


class OMLXClient:
    """oMLX API 客户端（同步 + 异步）"""

    def __init__(self, base_url: str, api_key: str = "dummy", timeout: float = 60.0):
        """
        初始化客户端

        Args:
            base_url: API 基础 URL
            api_key: API 密钥
            timeout: 超时时间（秒）
        """
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout

        # 同步 / 异步客户端（懒加载）
        self._sync_client: Optional[OpenAI] = None
        self._async_client: Optional[AsyncOpenAI] = None
        # 懒加载双检锁：避免多线程 concurrently 首次访问时重复创建客户端
        import threading
        self._client_lock = threading.Lock()
        # 最近一次聊天的 usage（tokens / toks/s；流式为 include_usage 末块）。
        # D6-1 后仅作诊断用途：功能消费方必须通过各 chat 方法的 usage_out
        # 参数按请求读取（共享属性在并发下会串号）。
        self.last_chat_usage: Optional[Dict[str, Any]] = None

    # oMLX 服务端（uvicorn）在 HTTP keep-alive 连接复用时，同一连接的
    # 第二个及以后请求会返回 404（实测 httpx 复现：200→404→404...）。
    # 强制每请求使用独立连接规避；本地回环地址下连接开销可忽略。
    _KEEPALIVE_BYPASS_HEADERS = {"Connection": "close"}

    @property
    def sync(self) -> OpenAI:
        """获取同步客户端（双检锁懒加载，多线程安全）"""
        if self._sync_client is None:
            with self._client_lock:
                if self._sync_client is None:
                    self._sync_client = OpenAI(
                        base_url=self.base_url,
                        api_key=self.api_key,
                        timeout=self.timeout,
                        default_headers=self._KEEPALIVE_BYPASS_HEADERS,
                    )
        return self._sync_client

    @property
    def async_client(self) -> AsyncOpenAI:
        """获取异步客户端（双检锁懒加载，多线程安全）"""
        if self._async_client is None:
            with self._client_lock:
                if self._async_client is None:
                    self._async_client = AsyncOpenAI(
                        base_url=self.base_url,
                        api_key=self.api_key,
                        timeout=self.timeout,
                        default_headers=self._KEEPALIVE_BYPASS_HEADERS,
                    )
        return self._async_client

    # ================================================================
    # 嵌入向量
    # ================================================================

    def embed_sync(self, model: str, texts: List[str]) -> List[List[float]]:
        """
        同步生成嵌入向量

        Args:
            model: 嵌入模型名称
            texts: 文本列表

        Returns:
            List[List[float]]: 向量列表
        """
        response = self.sync.embeddings.create(
            model=model,
            input=texts,
        )
        return [item.embedding for item in response.data]

    async def embed_async(self, model: str, texts: List[str]) -> List[List[float]]:
        """
        异步生成嵌入向量

        Args:
            model: 嵌入模型名称
            texts: 文本列表

        Returns:
            List[List[float]]: 向量列表
        """
        response = await self.async_client.embeddings.create(
            model=model,
            input=texts,
        )
        return [item.embedding for item in response.data]

    # ================================================================
    # 聊天补全
    # ================================================================

    @staticmethod
    def _usage_to_dict(usage) -> Optional[Dict[str, Any]]:
        """把 OpenAI SDK 的 usage 对象转成可序列化 dict（含 oMLX 扩展字段）"""
        if usage is None:
            return None
        try:
            return {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
                "generation_tokens_per_second": getattr(usage, "generation_tokens_per_second", None),
                "time_to_first_token": getattr(usage, "time_to_first_token", None),
                "generation_duration": getattr(usage, "generation_duration", None),
                "total_time": getattr(usage, "total_time", None),
            }
        except Exception:
            return None

    @staticmethod
    def _retryable(err: Exception) -> bool:
        """可重试：5xx 或传输层错误（连接/超时）；4xx（含 429）不重试"""
        status = getattr(getattr(err, "response", None), "status_code", None)
        if status is not None:
            return status >= 500
        # 无 response：一般是传输层（连接失败/超时）
        return True

    def _retry_sync(self, fn, attempts: int = 2, backoff: float = 0.5):
        """同步调用重试（oMLX 本地偶发 5xx/抖动 → 自动重试 1 次）"""
        import time as _t
        last: Optional[Exception] = None
        for i in range(max(1, attempts)):
            try:
                return fn()
            except Exception as e:  # noqa: BLE001
                last = e
                if not self._retryable(e) or i == attempts - 1:
                    raise
                _t.sleep(backoff)
        raise last  # pragma: no cover

    async def _retry_async(self, fn, attempts: int = 2, backoff: float = 0.5):
        """异步调用重试"""
        import asyncio as _a
        last: Optional[Exception] = None
        for i in range(max(1, attempts)):
            try:
                return await fn()
            except Exception as e:  # noqa: BLE001
                last = e
                if not self._retryable(e) or i == attempts - 1:
                    raise
                await _a.sleep(backoff)
        raise last  # pragma: no cover

    def chat_sync(self, model: str, messages: List[Dict[str, str]],
                  usage_out: Optional[Dict[str, Any]] = None, **kwargs) -> str:
        """
        同步聊天（非流式）

        Args:
            model: 聊天模型名称
            messages: 消息列表
            usage_out: 调用方持有的局部 dict；usage 同时写入其中（D6-1：
                供 per-request 读取，避免共享属性 last_chat_usage 的并发串号；
                旧属性保留仅作诊断用途）
            **kwargs: 其他参数（max_tokens、temperature 等）

        Returns:
            str: 回复内容
        """
        response = self._retry_sync(lambda: self.sync.chat.completions.create(
            model=model,
            messages=messages,
            **kwargs
        ))
        usage = self._usage_to_dict(getattr(response, "usage", None))
        self.last_chat_usage = usage
        if usage_out is not None:
            usage_out["usage"] = usage
        return response.choices[0].message.content

    async def chat_async(
        self,
        model: str,
        messages: List[Dict[str, str]],
        usage_out: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> str:
        """
        异步聊天（非流式）

        Args:
            model: 聊天模型名称
            messages: 消息列表
            usage_out: 调用方持有的局部 dict；usage 同时写入其中（同 chat_sync，D6-1）
            **kwargs: 其他参数

        Returns:
            str: 回复内容
        """
        response = await self._retry_async(lambda: self.async_client.chat.completions.create(
            model=model,
            messages=messages,
            **kwargs
        ))
        usage = self._usage_to_dict(getattr(response, "usage", None))
        self.last_chat_usage = usage
        if usage_out is not None:
            usage_out["usage"] = usage
        return response.choices[0].message.content

    # ================================================================
    # 流式聊天
    # ================================================================

    def chat_stream_sync(
        self,
        model: str,
        messages: List[Dict[str, str]],
        usage_out: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> Generator[str, None, None]:
        """
        同步流式聊天

        Args:
            model: 聊天模型名称
            messages: 消息列表
            usage_out: 调用方持有的局部 dict；流末块的 usage 同时写入其中（D6-1）
            **kwargs: 其他参数

        Yields:
            str: 流式输出的文本片段
        """
        # include_usage：流末块携带 token 用量（oMLX 已支持），用于 toks/s 展示
        kwargs.setdefault("stream_options", {"include_usage": True})
        stream = self._retry_sync(lambda: self.sync.chat.completions.create(
            model=model,
            messages=messages,
            stream=True,
            **kwargs
        ))

        try:
            for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage = self._usage_to_dict(chunk.usage)
                    self.last_chat_usage = usage
                    if usage_out is not None:
                        usage_out["usage"] = usage
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        finally:
            # 与异步版一致：显式关闭底层响应，避免异常/提前退出时依赖 GC 释放连接
            close = getattr(stream, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass

    async def chat_stream_async(
        self,
        model: str,
        messages: List[Dict[str, str]],
        usage_out: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> AsyncGenerator[str, None]:
        """
        异步流式聊天

        基于 AsyncOpenAI 官方 SDK。关键点：流式响应迭代结束后，
        必须在事件循环存活时于 finally 中显式调用 stream.close()，
        及时关闭底层 httpx 响应；否则流对象会在事件循环关闭后的
        垃圾回收阶段被释放，触发 Python 3.13 + httpx 的
        "generator didn't stop after athrow()" 兼容性故障
        （详见 docs/async-stream-issue.md）。

        Args:
            model: 聊天模型名称
            messages: 消息列表
            usage_out: 调用方持有的局部 dict；流末块的 usage 同时写入其中（D6-1）
            **kwargs: 其他参数

        Yields:
            str: 流式输出的文本片段
        """
        kwargs.setdefault("stream_options", {"include_usage": True})
        stream = await self._retry_async(lambda: self.async_client.chat.completions.create(
            model=model,
            messages=messages,
            stream=True,
            **kwargs
        ))

        try:
            async for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage = self._usage_to_dict(chunk.usage)
                    self.last_chat_usage = usage
                    if usage_out is not None:
                        usage_out["usage"] = usage
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        finally:
            # 在事件循环存活期间显式关闭底层 httpx 响应流
            await stream.close()

    # ================================================================
    # 其他
    # ================================================================

    def get_models(self) -> List[str]:
        """获取可用模型列表"""
        response = self.sync.models.list()
        return [model.id for model in response.data]