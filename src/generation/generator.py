"""
生成服务
调用聊天模型生成回答，同步/异步双通道，均支持流式输出
"""

from typing import List, Dict, Any, Optional, Generator, AsyncGenerator

from src.embedding.client import OMLXClient
from src.generation.model_router import ModelRouter

from src.logging_setup import get_logger

logger = get_logger(__name__)


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
        # 最近一次生成的 usage（tokens / toks/s，来自 oMLX）。
        # D6-1 后仅作诊断用途：并发下共享属性会串号，功能消费方必须通过
        # 各生成方法的 usage_out 参数按请求读取。
        self.last_usage: Optional[Dict[str, Any]] = None

        # 默认系统提示词
        self.default_system_prompt = (
            "你是一个知识助手，基于提供的上下文信息回答问题。\n"
            "如果上下文中有相关信息，请引用它们。\n"
            "如果上下文中没有相关信息，请如实告知。\n"
            "回答要简洁、准确、有条理。\n"
            "使用中文回答。"
        )
        self.system_prompt = system_prompt or self.default_system_prompt
        self.answer_style: str = "balanced"  # brief | balanced | detailed（提示词层控制回答长短）

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
        usage_out: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> str:
        """
        同步生成回答（非流式）

        Args:
            query: 用户问题
            context: 上下文文本
            history: 对话历史
            usage_out: 调用方持有的局部 dict；本次生成的 usage 写入其中（D6-1，
                per-request 读取路径；self.last_usage 保留仅作诊断用途）
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

        usage_holder: Dict[str, Any] = {}
        text = self.client.chat_sync(usage_out=usage_holder, **params)
        # 读 per-call holder 而非共享属性（D6-1：并发下共享属性会串号）
        self.last_usage = usage_holder.get("usage")
        if usage_out is not None:
            usage_out["usage"] = self.last_usage
        return text

    def generate_stream(
        self,
        query: str,
        context: str,
        history: Optional[List[Dict[str, str]]] = None,
        usage_out: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> Generator[str, None, None]:
        """
        同步流式生成回答

        Args:
            query: 用户问题
            context: 上下文文本
            history: 对话历史
            usage_out: 调用方持有的局部 dict；流结束后写入本次 usage（D6-1）
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

        usage_holder: Dict[str, Any] = {}
        for chunk in self.client.chat_stream_sync(usage_out=usage_holder, **params):
            yield chunk
        # 流结束后取用量（流末块携带，含真实 toks/s）；读 per-call holder（D6-1）
        self.last_usage = usage_holder.get("usage")
        if usage_out is not None:
            usage_out["usage"] = self.last_usage

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
        usage_out: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> str:
        """
        异步生成回答（非流式）

        Args:
            query: 用户问题
            context: 上下文文本
            history: 对话历史
            usage_out: 调用方持有的局部 dict；本次生成的 usage 写入其中（D6-1）
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

        usage_holder: Dict[str, Any] = {}
        text = await self.client.chat_async(usage_out=usage_holder, **params)
        # 读 per-call holder 而非共享属性（D6-1）
        self.last_usage = usage_holder.get("usage")
        if usage_out is not None:
            usage_out["usage"] = self.last_usage
        return text

    async def generate_stream_async(
        self,
        query: str,
        context: str,
        history: Optional[List[Dict[str, str]]] = None,
        usage_out: Optional[Dict[str, Any]] = None,
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
            usage_out: 调用方持有的局部 dict；流结束后写入本次 usage（D6-1）
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

        usage_holder: Dict[str, Any] = {}
        async for chunk in self.client.chat_stream_async(usage_out=usage_holder, **params):
            yield chunk
        # 流结束后取用量（流末块携带，含真实 toks/s）。
        # D6-1：必须读 per-call holder——纯事件循环路径上两个交错协程共享
        # 同一线程，threading.local 无效，client 的共享写点与这里的读取点
        # 之间存在挂起窗口，读共享属性会拿到并发请求的 usage。
        self.last_usage = usage_holder.get("usage")
        if usage_out is not None:
            usage_out["usage"] = self.last_usage

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
            logger.warning("追问改写失败，使用原问题: %s", e)
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
            logger.warning("追问改写失败，使用原问题: %s", e)
            return question

    # ================================================================
    # 内部工具
    # ================================================================

    BRIEF_SUFFIX = (
        "回答务必简要：只给结论与要点，避免冗长展开；"
        "优先用 3~5 条短列表，不要写长段落、不要重复已答内容。"
    )
    DETAILED_SUFFIX = "回答要完整详实：充分展开每个要点，可补充背景与示例。"

    def _effective_system_prompt(self) -> str:
        """按 answer_style 叠加提示词（回答长短控制在提示词层，是生成耗时的主要杠杆）"""
        if self.answer_style == "brief":
            return f"{self.system_prompt}\n{self.BRIEF_SUFFIX}"
        if self.answer_style == "detailed":
            return f"{self.system_prompt}\n{self.DETAILED_SUFFIX}"
        return self.system_prompt

    def _build_messages(
        self,
        query: str,
        context: str,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> List[Dict[str, str]]:
        """构建消息列表（系统提示词 + 裁剪后的对话历史 + 用户问题）"""
        messages = []

        # 系统提示词（按回答风格叠加：brief/detailed 改变回答长短，生成耗时随之变化）
        messages.append({
            "role": "system",
            "content": self._effective_system_prompt()
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