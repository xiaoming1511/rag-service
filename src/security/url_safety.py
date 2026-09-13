"""
URL 安全校验（SSRF 防护）

供「抓取远程网页 → 索引」路径复用，防止服务被用于访问内网/回环/
云元数据等敏感目标。仅允许 http/https，且目标 IP 不得为：
- 未指定地址（0.0.0.0 / ::）
- 回环（loopback）
- 私网（private，如 10.x / 172.16-31.x / 192.168.x）
- 链路本地（link-local，169.254.x，含 AWS/GCP 云元数据）
- 保留地址（reserved）

放行开关（默认关闭，即默认拒绝内网）：
- 环境变量 RAG_ALLOW_PRIVATE_URLS=1
- 或配置 config/settings.yaml 的 security.allow_private_urls: true
  用途：离线测试（本地 HTTP 服务）、索引自己内网的 Wiki/文档站。
  该配置**不**在 /v1/config 的可改白名单内，避免被远程热改为放行。

已知限制（TOCTOU / DNS rebinding）：本函数解析一次 DNS 做校验，下游
httpx 抓取时会再次解析。攻击者若控制权威 DNS 并让两次解析返回不同 IP，
理论上可绕过。当前为单用户本地服务、且来源 URL 由用户自己提供，
风险可接受；若未来对外暴露，应改为「校验后固定 IP 再抓取」
（httpx 的 transport 层 pin IP + Host 头保留原域名）。
"""

import ipaddress
import os
import socket
from typing import Optional
from urllib.parse import urlparse


class UnsafeURLError(ValueError):
    """URL 指向内网/回环/元数据等禁止访问的目标"""


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def allow_private_urls() -> bool:
    """
    是否放行内网/回环地址（每次调用实时读取，便于测试与环境切换）

    优先级：环境变量 RAG_ALLOW_PRIVATE_URLS > 配置 security.allow_private_urls > False
    环境变量显式设置为非真值（如 0/false）时同样生效，便于临时关闭。
    """
    env = os.getenv("RAG_ALLOW_PRIVATE_URLS")
    if env is not None:
        return _truthy(env)
    try:
        from src.config import get_config
        return bool(get_config().security.allow_private_urls)
    except Exception:
        # 配置未加载（如单测直接调用）时按安全默认值处理
        return False


def validate_public_url(url: str, allow_private: Optional[bool] = None) -> None:
    """
    校验 URL 仅 http/https 且目标非内网/回环/元数据地址。

    Args:
        url: 待校验的 URL
        allow_private: 是否放行内网/回环；None 表示按 allow_private_urls() 自动判定

    Raises:
        UnsafeURLError: 协议不允许，或目标 IP 属于禁止访问范围
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError(f"仅支持 http/https 协议: {url}")

    host = parsed.hostname
    if not host:
        raise UnsafeURLError(f"URL 缺少主机名: {url}")

    if allow_private is None:
        allow_private = allow_private_urls()
    if allow_private:
        return

    # 1. host 本身是 IP 字面量
    #    注意：ip_address() 解析与 _reject_unsafe_ip() 必须分开 try 捕获——
    #    UnsafeURLError 继承自 ValueError，若把拒绝逻辑放进同一个
    #    `except ValueError` 里，拒绝异常会被当成「不是 IP 字面量」吞掉。
    literal_ip = None
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        _reject_unsafe_ip(literal_ip, url)
        return

    # 2. host 是域名，解析后逐一校验
    try:
        resolved = socket.getaddrinfo(host, None)
    except Exception:
        # DNS 解析失败：交由下游抓取时报错，此处不阻断
        return

    for r in resolved:
        ip_str = r[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        _reject_unsafe_ip(ip, url)


def _reject_unsafe_ip(ip, url: str) -> None:
    # is_unspecified 需显式判断：0.0.0.0 / :: 在不同 Python 版本下
    # is_private / is_reserved 的判定并不稳定，单独列出避免漏网
    if (ip.is_unspecified or ip.is_loopback or ip.is_private
            or ip.is_link_local or ip.is_reserved):
        raise UnsafeURLError(f"禁止访问内网/回环/元数据地址: {url}")
