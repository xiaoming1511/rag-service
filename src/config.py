"""
配置管理模块
加载 YAML 配置文件，提供类型安全的配置访问
"""

import os
from pathlib import Path
from typing import Optional, List

import yaml
from pydantic import BaseModel, Field


class OMLXConfig(BaseModel):
    """oMLX API 配置"""
    base_url: str = "http://127.0.0.1:8000/v1"
    api_key: str = "dummy"
    chat_model: str = "qwen3.5-4b-mlx-4bit"
    embedding_model: str = "bge-m3-mlx-4bit"
    reranker_model: str = "bge-reranker-v2-m3"
    timeout: float = 60.0


class ChunkerConfig(BaseModel):
    """文档分块配置"""
    chunk_size: int = 800
    overlap: int = 100
    strategy: str = "heading"  # 分块策略：heading（按标题）| fixed（按固定大小）


class RetrievalConfig(BaseModel):
    """检索配置"""
    top_k: int = 5
    rerank_top_k: int = 3
    enable_rerank: bool = True
    similarity_threshold: float = 0.5
    rerank_threshold: float = 0.0  # 重排序后分数阈值（低于丢弃）；0 = 关闭，建议评测基线后调参
    strict_sources: bool = False  # 严格来源模式：检索为空时不调用模型，直接告知未找到
    context_token_budget: int = 4000  # 上下文 token 预算（B2：替代字符硬截断）
    hybrid: bool = False              # 混合检索（B3）：已实现，实测小语料收益≈零，扩容后开启
    hybrid_candidates: int = 20       # 混合检索每路召回候选数
    rrf_k: int = 60                   # RRF 融合常数
    parent_expansion: bool = True     # 父子块召回（B4，方案 A）：命中小块实时聚合父块入上下文
    parent_max_tokens: int = 1600     # 单个父块 token 上限（超出以命中块为中心截窗）
    # —— 检索保真修复（recall 面放大的选项）——
    recall_candidates: int = 30       # 召回候选窗（rerank 前每路抓取数）。
                                      # 旧设计在 hybrid=off 时召回数=top_k(5)，导致
                                      # 高分历史沉淀霸榜、真实块排到候选窗外被漏掉。
                                      # 提高后让 rerank 有机会捞回真实高分块。
    rerank_candidates: int = 8        # rerank 候选池上限：只精排召回窗内前 N 个
                                      # （按降权后相似度取 TopN）。旧实现重排全部候选
                                      # （最多 recall_candidates 个），本地 rerank 一次
                                      # 30 对耗时可达 ~6s；收窄到 8 对即可在保住真实
                                      # 高分块的同时大幅降延迟。应 >= rerank_top_k。
    synthesis_weight: float = 0.85    # 问答沉淀(syntheses 目录文件)块的相关性权重，
                                      # 0~1。用于打压"历史问答以问代答"对 top 榜的占领。
                                      # 1.0 = 不过滤；建议 0.6~0.9；0 = 完全排除。


class AuthConfig(BaseModel):
    """API 认证配置（预留：公网开放时启用，Obsidian 插件已默认发送 Bearer 头）"""
    enabled: bool = False  # False = 不校验（当前本地行为不变）
    api_key: str = ""      # 启用后校验请求头 Authorization: Bearer <api_key>


class RateLimitConfig(BaseModel):
    """简易限流（每 IP 滑动窗口；默认关闭，仅外网/共享环境启用）"""
    enabled: bool = False        # False = 不限流（本地默认为关）
    max_per_minute: int = 60     # 每 IP 每分钟最大请求数（窗口 1 分钟，内存态）


class SecurityConfig(BaseModel):
    """抓取安全配置（SSRF 防护）"""
    # False（默认）= 拒绝抓取内网/回环/云元数据地址；
    # True = 放行（离线测试本地 HTTP 服务、索引自建内网文档站时使用）。
    # 也可用环境变量 RAG_ALLOW_PRIVATE_URLS=1 临时覆盖（优先级更高）。
    # 该项不在 /v1/config 的可改白名单内，防止远程热改为放行。
    allow_private_urls: bool = False


class SynthesesConfig(BaseModel):
    """问答沉淀配置（syntheses）"""
    enabled: bool = True
    dir: str = ""  # 沉淀目录；空 = 自动（source_dirs[0]/syntheses，会被加载器索引）


class OCRConfig(BaseModel):
    """
    OCR 配置（用视觉模型对图片 / 扫描件做文字识别）

    默认**关闭**：实测单页约 8s（冷加载 4.9s + 出字 2.4s），开启后首次索引
    明显变慢，且会与问答争抢同一台 oMLX 服务。关闭时全链路行为与未接入前
    完全一致（扫描件照旧被跳过、图片照旧不作为文档加载）。

    开启方式（两步，缺一不可）：
      1) `ocr.enabled: true`
      2) 把要纳入的图片扩展名加进 `documents.supported_extensions`
         （如 .png / .jpg），否则加载器根本不会扫描到图片文件。
    """
    enabled: bool = False
    model: str = "OvisOCR2"
    base_url: str = ""          # 空 = 复用 omlx.base_url
    api_key: str = ""           # 空 = 复用 omlx.api_key
    timeout: float = 120.0      # 单图/单页请求超时（OCR 比聊天慢，默认给足）
    prompt: str = "OCR"         # 送模型的指令；OvisOCR2 实测对 "OCR" 直接返回 Markdown

    # —— 扫描页判定 ——
    min_text_chars: int = 16    # PDF 页面文本层字符数低于此值 → 视为扫描页，整页 OCR

    # —— 成本护栏（防止一次索引把模型服务打满）——
    render_dpi: int = 150            # 扫描页渲染 DPI（过高会显著放大请求体）
    max_pages_per_doc: int = 30      # 单文档最多 OCR 页数，超出跳过并记日志
    max_images_per_doc: int = 20     # 单文档最多 OCR 内嵌图片数
    min_image_side: int = 64         # 最小边长（像素），用于跳过图标/装饰线
    max_image_pixels: int = 40_000_000  # 单图最大像素（约 40MP），防解压炸弹


class RoutingConfig(BaseModel):
    """
    模型路由配置（模型路由：让不同任务使用不同模型）

    空值（""）= 跟随默认聊天模型（omlx.chat_model）。
    - chat:               问答生成
    - rewrite:            多轮追问改写
    - research_subqueries: Deep Research 子查询拆解
    """
    chat: str = ""
    rewrite: str = ""
    research_subqueries: str = ""


class GenerationConfig(BaseModel):
    """生成配置"""
    max_tokens: int = 512
    temperature: float = 0.3
    stream: bool = True
    max_history_rounds: int = 10        # 多轮对话保留的最大轮数
    history_token_budget: int = 2000    # 历史 token 预算，超出从旧到新裁剪
    rewrite_query: bool = False         # 是否启用追问改写（默认关闭）
    answer_style: str = "balanced"      # brief | balanced | detailed（提示词层控制回答长短）


class VectorStoreConfig(BaseModel):
    """向量存储配置"""
    collection_name: str = "knowledge_base"
    persist_directory: str = "./data/chroma_db"


class DocumentsConfig(BaseModel):
    """文档源配置"""
    source_dirs: List[str] = Field(default_factory=list)
    supported_extensions: List[str] = Field(default_factory=lambda: [".md", ".markdown"])


class PerformanceConfig(BaseModel):
    """性能优化配置（决策 D7）"""
    embed_cache_capacity: int = 4096    # 嵌入内存 LRU 缓存容量（0 关闭）
    index_max_workers: int = 0          # 索引并行分块线程数（0 自动，1 串行）
    response_cache: bool = True         # 相同问题响应缓存开关
    response_cache_ttl: int = 3600      # 响应缓存有效期（秒）


class AppConfig(BaseModel):
    """应用总配置"""
    omlx: OMLXConfig = Field(default_factory=OMLXConfig)
    chunker: ChunkerConfig = Field(default_factory=ChunkerConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    vector_store: VectorStoreConfig = Field(default_factory=VectorStoreConfig)
    documents: DocumentsConfig = Field(default_factory=DocumentsConfig)
    performance: PerformanceConfig = Field(default_factory=PerformanceConfig)
    syntheses: SynthesesConfig = Field(default_factory=SynthesesConfig)
    ocr: OCRConfig = Field(default_factory=OCRConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)


class ConfigManager:
    """配置管理器（单例模式）"""

    _instance: Optional["ConfigManager"] = None
    _config: Optional[AppConfig] = None
    _config_path: Optional[Path] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def load(self, config_path: str = "config/settings.yaml") -> AppConfig:
        """加载配置文件"""
        config_file = Path(config_path)

        # 如果不是绝对路径，基于项目根目录解析
        if not config_file.is_absolute():
            # 获取项目根目录（src/config.py 的父目录）
            project_root = Path(__file__).parent.parent
            config_file = project_root / config_path

        if not config_file.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_file}")

        with open(config_file, "r", encoding="utf-8") as f:
            raw_config = yaml.safe_load(f)

        # 空 YAML 文件 → 空配置字典（交由 AppConfig 校验字段缺失）
        if raw_config is None:
            raw_config = {}
        if not isinstance(raw_config, dict):
            raise ValueError(f"配置文件顶层必须是映射，得到: {type(raw_config).__name__}")

        # 处理环境变量覆盖
        raw_config.setdefault("omlx", {})
        if os.getenv("OMLX_BASE_URL"):
            raw_config["omlx"]["base_url"] = os.getenv("OMLX_BASE_URL")
        if os.getenv("OMLX_CHAT_MODEL"):
            raw_config["omlx"]["chat_model"] = os.getenv("OMLX_CHAT_MODEL")

        self._config = AppConfig(**raw_config)
        self._config_path = config_file
        return self._config

    def replace(self, new_config: AppConfig) -> AppConfig:
        """整体替换内存配置（供 /v1/config 热更新使用，需再调用 save() 持久化）"""
        self._config = new_config
        return self._config

    def save(self, path: Optional[str] = None) -> Path:
        """
        把当前配置写回 YAML 文件

        Args:
            path: 目标路径；默认写回加载时的配置文件

        Returns:
            Path: 写入的文件路径

        Note:
            写回会丢失原文件的注释（程序生成的规范化 YAML）
        """
        target = Path(path) if path else self._config_path
        if target is None or self._config is None:
            raise RuntimeError("尚未加载配置，无法保存")
        target.parent.mkdir(parents=True, exist_ok=True)

        # 原子写：先写临时文件再 os.replace，避免写入中断截断配置文件
        import tempfile
        fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.safe_dump(self._config.model_dump(), f, allow_unicode=True, sort_keys=False)
            os.replace(tmp_path, target)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return target

    @property
    def config(self) -> AppConfig:
        """获取配置"""
        if self._config is None:
            raise RuntimeError("请先调用 load() 加载配置")
        return self._config


# 全局配置管理器实例
config_manager = ConfigManager()


def get_config() -> AppConfig:
    """获取配置的便捷函数"""
    return config_manager.config