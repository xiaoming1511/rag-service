"""
API 请求/响应数据模型
"""

from typing import List, Optional, Dict, Any
from pydantic import BaseModel


class QueryRequest(BaseModel):
    """问答请求"""
    question: str
    top_k: Optional[int] = 5
    use_rerank: Optional[bool] = True
    history: Optional[List[Dict[str, str]]] = None


class SourceInfo(BaseModel):
    """来源信息（file_path/heading 供 Obsidian 插件点击跳转使用，向后兼容保留）"""
    file_name: str
    content: str
    score: float
    file_path: Optional[str] = None  # 文件绝对路径（插件据此换算 vault 相对路径）
    heading: Optional[str] = None    # 标题路径锚点（如 "基础语法 > 变量"）


class QueryResponse(BaseModel):
    """问答响应"""
    answer: str
    sources: List[SourceInfo]
    total_results: int


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
    message: str


class IndexUrlRequest(BaseModel):
    """网页索引请求"""
    url: str
    timeout: Optional[float] = 30.0


class IndexUrlResponse(BaseModel):
    """网页索引响应"""
    success: bool
    url: str
    title: Optional[str] = None
    chunk_count: int = 0
    message: str


class StatusResponse(BaseModel):
    """状态响应"""
    status: str
    vector_count: int
    collection_name: str
    config: Dict[str, Any]


class ErrorResponse(BaseModel):
    """错误响应"""
    error: str
    detail: Optional[str] = None