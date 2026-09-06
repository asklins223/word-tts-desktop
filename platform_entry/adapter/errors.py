"""Errors shared by validation, page actions, and the runner."""


class PlatformInputError(RuntimeError):
    """本地输入、页面状态或登录态错误。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.steps: list[dict] = []
        self.feedback: dict = {}


class PlatformInputLoginError(PlatformInputError):
    """在限定时间内没有等到管理端登录完成。"""


class PlatformInputUiError(PlatformInputError):
    """页面控件不存在、不可用或页面反馈失败。"""


class PlatformInputExistingPaperIncompatibleError(PlatformInputUiError):
    """既有试卷的基础分类与本次录入目标不兼容。"""
