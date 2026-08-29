"""讯飞配音引擎桥接：统一守卫导入，供合成与批量模块引用。"""

try:
    import xunfei as _xunfei
    _XUNFEI_AVAILABLE = _xunfei.is_available()
except Exception:
    _XUNFEI_AVAILABLE = False
    _xunfei = None



