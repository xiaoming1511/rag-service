"""
生成服务
调用聊天模型生成回答，同步/异步双通道，均支持流式输出
"""

from typing import List, Dict, Any, Optional, Generator, AsyncGenerator

from src.embedding.client import OMLXClient


class Generator:
    """生成服务（同步 + 异步）"""

    def __init__(
        self,
        client: OMLXClient,
        model: str,
        max_tokens: int = 512,
        temperature: float = 0.3,
        stream: bool = True,
        system_prompt: Optional[str] = None,
    ):
        """
        初始化生成服务

        Args:
            client: oMLX 客户端
            model: 聊天模型名称
            max_tokens: 最大输出 token 数
            temperature: 温度参数
            stream: 是否启用流式输出
            system_prompt: 系统提示词（默认使用内置中文提示词）
        """
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.stream = stream

        # 默认系统提示词
        self.default_system_prompt = (
            "你是一个知识助手，基于提供的上下文信息回答问题。\n"
            "如果上下文中有相关信息，请引用它们。\n"
            "如果上下文中没有相关信息，请如实告知。\n"
            "回答要简洁、准确、有条理。\n"
            "使用中文回答。"
        )
        self.system_prompt = system_prompt or self.default_system_prompt

    # ================================================================
    # 同步
    # ================================================================

    def generate(
        self,
        query: str,
        context: str,
        history: Optional[List[Dict[str, str]]] = None,
        **kwargs
    ) -> str:
        """
        同步生成回答（非流式）

        Args:
            query: 用户问题
            context: 上下文文本
            history: 对话历史
            **kwargs: 其他参数

        Returns:
            str: 生成的回答
        """
        messages = self._build_messages(query, context, history)

        params = {
            "model": self.model,
            "messages": messages,
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "temperature": kwargs.get("temperature", self.temperature),
        }

        return self.client.chat_sync(**params)

    def generate_stream(
        self,
        query: str,
        context: str,
        history: Optional[List[Dict[str, str]]] = None,
        **kwargs
    ) -> Generator[str, None, None]:
        """
        同步流式生成回答

        Args:
            query: 用户问题
            context: 上下文文本
            history: 对话历史
            **kwargs: 其他参数

        Yields:
            str: 流式输出的文本片段
        """
        messages = self._build_messages(query, context, history)

        params = {
            "model": self.model,
            "messages": messages,
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "temperature": kwargs.get("temperature", self.temperature),
        }

        for chunk in self.client.chat_stream_sync(**params):
            yield chunk

    # ========== 同步别名方法，保持接口兼容 ==========
    def generate_stream_sync(
        self,
        query: str,
        context: str,
        history: Optional[List[Dict[str, str]]] = None,
        **kwargs
    ) -> Generator[str, None, None]:
        """
        generate_stream 的别名（向后兼容）
        """
        return self.generate_stream(query, context, history, **kwargs)

    # ================================================================
    # 异步
    # ================================================================

    async def generate_async(
        self,
        query: str,
        context: str,
        history: Optional[List[Dict[str, str]]] = None,
        **kwargs
    ) -> str:
        """
        异步生成回答（非流式）

        Args:
            query: 用户问题
            context: 上下文文本
            history: 对话历史
            **kwargs: 其他参数

        Returns:
            str: 生成的回答
        """
        messages = self._build_messages(query, context, history)

        params = {
            "model": self.model,
            "messages": messages,
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "temperature": kwargs.get("temperature", self.temperature),
        }

        return await self.client.chat_async(**params)

    async def generate_stream_async(
        self,
        query: str,
        context: str,
        history: Optional[List[Dict[str, str]]] = None,
        **kwargs
    ) -> AsyncGenerator[str, None]:
        """
        异步流式生成回答

        基于 AsyncOpenAI 官方 SDK，规避 Python 3.13 下
        httpx 流式生成器清理的兼容性问题。

        Args:
            query: 用户问题
            context: 上下文文本
            history: 对话历史
            **kwargs: 其他参数

        Yields:
            str: 流式输出的文本片段
        """
        messages = self._build_messages(query, context, history)

        params = {
            "model": self.model,
            "messages": messages,
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "temperature": kwargs.get("temperature", self.temperature),
        }

        async for chunk in self.client.chat_stream_async(**params):
            yield chunk

    # ================================================================
    # 内部工具
    # ================================================================

    def _build_messages(
        self,
        query: str,
        context: str,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> List[Dict[str, str]]:
        """构建消息列表（系统提示词 + 对话历史 + 用户问题）"""
        messages = []

        # 系统提示词
        messages.append({
            "role": "system",
            "content": self.system_prompt
        })

        # 对话历史
        if history:
            messages.extend(history)

        # 用户问题（含上下文）
        user_content = self._build_user_content(query, context)
        messages.append({
            "role": "user",
            "content": user_content
        })

        return messages

    def _build_user_content(self, query: str, context: str) -> str:
        """构建用户消息内容（将检索到的上下文拼接到问题中）"""
        if not context:
            return f"问题：{query}\n\n（没有找到相关上下文信息，请根据你的知识回答。）"

        return f"""请基于以下上下文信息回答问题：

【上下文信息】
{context}

【问题】
{query}

【要求】
1. 如果上下文中有相关信息，优先使用上下文内容回答
2. 引用来源时标注来源文件名（如 [Python基础.md]）
3. 如果上下文信息不足，如实告知，可以补充你的知识
4. 回答简洁、有条理
"""