"""
生成服务
调用聊天模型生成回答，同步/异步双通道，均支持流式输出
"""

from typing import List, Dict, Any, Optional, Generator, AsyncGenerator

from src.embedding.client import OMLXClient
from src.generation.model_router import ModelRouter


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
        max_history_rounds: int = 10,
        history_token_budget: int = 2000,
        rewrite_query: bool = False,
        model_router: Optional[ModelRouter] = None,
    ):
        """
        初始化生成服务

        Args:
            client: oMLX 客户端
            model: 聊天模型名称（默认模型）
            max_tokens: 最大输出 token 数
            temperature: 温度参数
            stream: 是否启用流式输出
            system_prompt: 系统提示词（默认使用内置中文提示词）
            max_history_rounds: 多轮对话保留的最大轮数（决策 D6）
            history_token_budget: 历史 token 预算，超出从旧到新裁剪
            rewrite_query: 是否启用追问改写（决策 D5）：
                检索前用模型把追问改写为独立问句，默认关闭
            model_router: 模型路由（模型路由）：让不同任务使用不同模型；
                None 时所有任务使用 model
        """
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.stream = stream
        self.max_history_rounds = max_history_rounds
        self.history_token_budget = history_token_budget
        self.rewrite_query = rewrite_query
        self.model_router = model_router

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
    # 模型路由
    # ================================================================

    def model_for(self, task: str = "chat") -> str:
        """
        按任务解析应使用的模型（模型路由）

        Args:
            task: chat / rewrite / research_subqueries；未配置回退默认模型

        Returns:
            str: 模型名
        """
        if self.model_router is not None:
            return self.model_router.resolve(task)
        return self.model

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
            "model": kwargs.pop("model", self.model_for("chat")),
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
            "model": kwargs.pop("model", self.model_for("chat")),
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
            "model": kwargs.pop("model", self.model_for("chat")),
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
            "model": kwargs.pop("model", self.model_for("chat")),
            "messages": messages,
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "temperature": kwargs.get("temperature", self.temperature),
        }

        async for chunk in self.client.chat_stream_async(**params):
            yield chunk

    # ================================================================
    # 追问改写（决策 D5，默认关闭）
    # ================================================================

    REWRITE_SYSTEM_PROMPT = (
        "你是对话问题改写助手。请根据对话历史，把用户的追问改写为"
        "独立、完整、无歧义的问题（不引用「它/那个/这个」等代词）。\n"
        "只输出改写后的问题本身，不要任何解释或标点修饰。"
    )

    def rewrite_question(
        self,
        question: str,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        """
        同步改写追问为独立问题（未启用或无需改写时原样返回）

        Args:
            question: 用户当前问题
            history: 对话历史

        Returns:
            str: 改写后的问题（或原问题）
        """
        if not self.rewrite_query or not history or not question.strip():
            return question

        messages = [
            {"role": "system", "content": self.REWRITE_SYSTEM_PROMPT},
            *history[-6:],  # 改写只需最近几轮上下文
            {"role": "user", "content": f"追问：{question}"},
        ]
        try:
            rewritten = self.client.chat_sync(
                model=self.model_for("rewrite"),
                messages=messages,
                max_tokens=80,
                temperature=0.0,
            )
            rewritten = (rewritten or "").strip()
            return rewritten if rewritten else question
        except Exception as e:
            print(f"⚠️ 追问改写失败，使用原问题: {e}")
            return question

    async def rewrite_question_async(
        self,
        question: str,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        """异步改写追问为独立问题（未启用或无需改写时原样返回）"""
        if not self.rewrite_query or not history or not question.strip():
            return question

        messages = [
            {"role": "system", "content": self.REWRITE_SYSTEM_PROMPT},
            *history[-6:],
            {"role": "user", "content": f"追问：{question}"},
        ]
        try:
            rewritten = await self.client.chat_async(
                model=self.model_for("rewrite"),
                messages=messages,
                max_tokens=80,
                temperature=0.0,
            )
            rewritten = (rewritten or "").strip()
            return rewritten if rewritten else question
        except Exception as e:
            print(f"⚠️ 追问改写失败，使用原问题: {e}")
            return question

    # ================================================================
    # 内部工具
    # ================================================================

    def _build_messages(
        self,
        query: str,
        context: str,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> List[Dict[str, str]]:
        """构建消息列表（系统提示词 + 裁剪后的对话历史 + 用户问题）"""
        messages = []

        # 系统提示词
        messages.append({
            "role": "system",
            "content": self.system_prompt
        })

        # 对话历史（先按轮数与 token 预算裁剪）
        if history:
            from src.generation.history import trim_history
            trimmed = trim_history(
                history,
                max_rounds=self.max_history_rounds,
                token_budget=self.history_token_budget,
            )
            messages.extend(trimmed)

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