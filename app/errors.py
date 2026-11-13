"""自定义异常，统一映射为 HTTP 400 响应。"""


class ValidationError(Exception):
    """输入不满足约束时抛出。"""


class ScriptRejected(Exception):
    """SQL 脚本违反安全/确定性要求时抛出。"""


class JobError(Exception):
    """任务状态相关错误。"""
