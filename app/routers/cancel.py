"""「终止」按钮的服务端出口：登记一个取消请求。

  POST /api/cancel/{token}    请求终止某个流式任务（对话 / 技能 / 知识网络 pipeline）

为什么要显式接口：SSE 流是同步生成器，跑在 worker 线程里，客户端断开连接并不能
中断它（模型会继续输出到底、白烧 token）。前端点「终止」时先调这里置位标记，
流式循环在事件/步骤之间检查到标记就抛 Cancelled 提前退出，并由上层收尾
（对话落库已生成部分、pipeline 把当前步与整轮记为 cancelled）。

token 由前端生成并随请求体下发（如 ChatRequest.cancel_token），一个 token 对应一个任务；
任务结束后服务端释放令牌，此后该 token 再调用本接口返回 stopped=false。
"""

from fastapi import APIRouter

from harness import cancel as cancel_registry

router = APIRouter(prefix="/api", tags=["cancel"])


@router.post("/cancel/{token}")
def cancel_task(token: str):
    """请求终止：返回 stopped=false 表示该任务已结束（或 token 从未登记）。"""
    stopped = cancel_registry.request_stop(token)
    return {"token": token, "stopped": stopped}
