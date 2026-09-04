============================================================

异步流式问题记录与解决方案 - Python 3.13 + httpx

============================================================

日期: 2026-09-04（首次记录）

项目: RAG Service (个人知识库)

问题描述
---------
在使用 httpx 直接进行异步流式请求时，Python 3.13 环境下出现：
RuntimeError: generator didn't stop after athrow()

触发场景
---------
- Python 3.13.0 + httpx 0.28.1 + httpcore 1.0.9
- async for 循环消费完流式响应后，自动调用 aclose()
- 底层 PoolByteStream 未能在收到 GeneratorExit 后及时停止
- 若流对象在事件循环关闭后的垃圾回收阶段才被释放，
  该错误会被打印到 stderr（虽不致崩溃，但污染日志）

曾经尝试且失败的方案
---------------------
1. ❌ 升级 httpx/httpcore 到当时最新版（仍复现）
2. ❌ 在 finally 块中先调用 stream.close() 再调用 agen.aclose()
3. ❌ 手动修复生成器清理顺序

最终解决方案（✅ 已验证）
-------------------------
改用 OpenAI 官方 SDK 的 AsyncOpenAI 异步客户端，并遵循两条关键约定：

1. 异步流式创建后，用 try/finally 包裹迭代过程，
   在 finally 中于事件循环存活时显式调用 stream.close()：
   同步关闭底层 httpx 响应，避免流对象延迟到
   事件循环关闭后才被回收。

        stream = await client.chat.completions.create(..., stream=True)
        try:
            async for chunk in stream:
                yield chunk.choices[0].delta.content
        finally:
            await stream.close()   # 关键：事件循环存活时显式关闭

2. 使用 AsyncOpenAI 而非裸 httpx，由 SDK 管理连接池与
   响应生命周期，减少底层时序问题。

验证结果（本机 oMLX 服务）
--------------------------
✅ 同步流式：正常
✅ 异步流式（完整消费）：正常，无 athrow 异常
✅ 异步流式（提前中断 + 显式 aclose）：正常，无 athrow 异常
✅ 异步嵌入 / 异步非流式聊天：正常

相关实现位置
-------------
- src/embedding/client.py    → chat_stream_async / chat_async / embed_async
- src/embedding/embedder.py  → embed_async（共享磁盘缓存）
- src/generation/generator.py→ generate_async / generate_stream_async
- src/pipeline/rag_pipeline.py → query_stream_async（检索走线程池 + 异步生成）
- src/api/routes/query.py    → /v1/query/stream 改用异步流式
- test/test_generator.py     → 异步流式回归测试

============================================================