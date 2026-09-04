"""
测试公共工具

提供测试共用的常量与辅助函数。
说明：需要连接本机 oMLX 服务的集成类测试，
在服务未启动时会自动跳过（通过模块级 pytestmark 声明）。
"""

import socket

# oMLX 服务地址（与 config/settings.yaml 保持一致）
OMLX_BASE_URL = "http://127.0.0.1:8000/v1"


def server_available() -> bool:
    """检测本机 oMLX 服务（127.0.0.1:8000）是否可用"""
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=2):
            return True
    except OSError:
        return False