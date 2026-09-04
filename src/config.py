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
    strict_sources: bool = False  # 严格来源模式：检索为空时不调用模型，直接告知未找到


class SynthesesConfig(BaseModel):
    """问答沉淀配置（syntheses）"""
    enabled: bool = True
    dir: str = ""  # 沉淀目录；空 = 自动（source_dirs[0]/syntheses，会被加载器索引）


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
    routing: RoutingConfig = Field(default_factory=RoutingConfig)


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

        # 处理环境变量覆盖
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
        with open(target, "w", encoding="utf-8") as f:
            yaml.safe_dump(self._config.model_dump(), f, allow_unicode=True, sort_keys=False)
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