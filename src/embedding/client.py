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

    @property
    def sync(self) -> OpenAI:
        """获取同步客户端"""
        if self._sync_client is None:
            self._sync_client = OpenAI(
                base_url=self.base_url,
                api_key=self.api_key,
                timeout=self.timeout,
            )
        return self._sync_client

    @property
    def async_client(self) -> AsyncOpenAI:
        """获取异步客户端"""
        if self._async_client is None:
            self._async_client = AsyncOpenAI(
                base_url=self.base_url,
                api_key=self.api_key,
                timeout=self.timeout,
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

    def chat_sync(self, model: str, messages: List[Dict[str, str]], **kwargs) -> str:
        """
        同步聊天（非流式）

        Args:
            model: 聊天模型名称
            messages: 消息列表
            **kwargs: 其他参数（max_tokens、temperature 等）

        Returns:
            str: 回复内容
        """
        response = self.sync.chat.completions.create(
            model=model,
            messages=messages,
            **kwargs
        )
        return response.choices[0].message.content

    async def chat_async(
        self,
        model: str,
        messages: List[Dict[str, str]],
        **kwargs
    ) -> str:
        """
        异步聊天（非流式）

        Args:
            model: 聊天模型名称
            messages: 消息列表
            **kwargs: 其他参数

        Returns:
            str: 回复内容
        """
        response = await self.async_client.chat.completions.create(
            model=model,
            messages=messages,
            **kwargs
        )
        return response.choices[0].message.content

    # ================================================================
    # 流式聊天
    # ================================================================

    def chat_stream_sync(
        self,
        model: str,
        messages: List[Dict[str, str]],
        **kwargs
    ) -> Generator[str, None, None]:
        """
        同步流式聊天

        Args:
            model: 聊天模型名称
            messages: 消息列表
            **kwargs: 其他参数

        Yields:
            str: 流式输出的文本片段
        """
        stream = self.sync.chat.completions.create(
            model=model,
            messages=messages,
            stream=True,
            **kwargs
        )

        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    async def chat_stream_async(
        self,
        model: str,
        messages: List[Dict[str, str]],
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
            **kwargs: 其他参数

        Yields:
            str: 流式输出的文本片段
        """
        stream = await self.async_client.chat.completions.create(
            model=model,
            messages=messages,
            stream=True,
            **kwargs
        )

        try:
            async for chunk in stream:
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