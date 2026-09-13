"""
API 请求/响应数据模型
"""

from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    """问答请求"""
    question: str = Field(..., min_length=1, max_length=10000)
    top_k: Optional[int] = Field(5, ge=1, le=100)
    use_rerank: Optional[bool] = True
    history: Optional[List[Dict[str, str]]] = None


class SourceInfo(BaseModel):
    """来源信息（file_path/heading/行号/图片 供 Obsidian 插件跳转与高亮）"""
    file_name: str
    content: str
    score: float
    file_path: Optional[str] = None    # 文件绝对路径（插件据此换算 vault 相对路径）
    heading: Optional[str] = None      # 标题路径锚点（如 "基础语法 > 变量"）
    line_start: Optional[int] = None   # 命中片段在原文中的起始行（1 起，行级引文）
    line_end: Optional[int] = None     # 命中片段在原文中的结束行
    images: Optional[List[Dict[str, Any]]] = None  # 附件图片 [{path, caption}]（多模态）


class QueryResponse(BaseModel):
    """问答响应"""
    answer: str
    sources: List[SourceInfo]
    total_results: int
    timing_ms: Optional[Dict[str, Any]] = None  # 分阶段耗时 {total_ms, retrieve_ms, generate_ms, ...}


class IndexRequest(BaseModel):
    """索引请求"""
    source_dirs: Optional[List[str]] = None
    rebuild: Optional[bool] = False


class IndexResponse(BaseModel):
    """索引响应"""
    success: bool
    total_documents: int
    total_chunks: int
    vector_count: int
    message: str


class IndexRefreshResponse(BaseModel):
    """增量索引响应"""
    success: bool
    added: int
    updated: int
    removed: int
    unchanged: int
    skipped: int = 0  # 已存在而跳过的文档（历史向量纳入清单管理）
    message: str


class IndexUrlRequest(BaseModel):
    """网页索引请求"""
    url: str = Field(..., min_length=1)
    timeout: Optional[float] = Field(30.0, ge=1.0, le=300.0)


class IndexUrlResponse(BaseModel):
    """网页索引响应"""
    success: bool
    url: str
    title: Optional[str] = None
    chunk_count: int = 0
    message: str


class JobRequest(BaseModel):
    """摄入任务提交请求"""
    kind: str = "incremental"  # full | incremental | url
    rebuild: Optional[bool] = False
    url: Optional[str] = None
    timeout: Optional[float] = Field(30.0, ge=1.0, le=300.0)


class JobResponse(BaseModel):
    """摄入任务对象"""
    id: str
    kind: str
    status: str
    progress: str = ""
    progress_data: Optional[Dict[str, Any]] = None  # 结构化进度 {stage,current,total,percent,note}
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    created_at: float
    started_at: Optional[float] = None
    finished_at: Optional[float] = None


class JobListResponse(BaseModel):
    """摄入任务列表"""
    jobs: List[JobResponse]
    total: int


class ResearchRequest(BaseModel):
    """深度研究请求"""
    question: str = Field(..., min_length=1, max_length=10000)
    sub_queries: Optional[List[str]] = None
    # 上限 5 与 DeepResearch 构造器里的硬夹 max(1, min(max_rounds, 5)) 对齐：
    # 声明 le=10 而实现夹到 5，会让调用方"要 8 轮得 5 轮"且毫无提示
    max_rounds: Optional[int] = Field(None, ge=1, le=5)  # 递归最大轮次（默认 2）


class ResearchResponse(BaseModel):
    """深度研究响应"""
    report: str
    sub_queries: List[str]
    sources: List[SourceInfo]
    total_results: int
    rounds: int = 1  # 实际执行轮次（可能因"本轮无新信息"提前停止，与请求值不同）


class StatusResponse(BaseModel):
    """状态响应"""
    status: str
    vector_count: int
    collection_name: str
    config: Dict[str, Any]
    index_job: Optional[Dict[str, Any]] = None  # 当前/最近摄入任务 {id,kind,status,progress,progress_data}
    metrics: Optional[Dict[str, Any]] = None    # 最近请求观测 {total,errors,avg/max_latency_ms,recent[]}
    rerank_cache: Optional[Dict[str, Any]] = None  # {hits,misses,hit_rate}


class ErrorResponse(BaseModel):
    """错误响应"""
    error: str
    detail: Optional[str] = None