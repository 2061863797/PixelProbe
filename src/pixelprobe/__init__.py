"""PixelProbe：本地图片与视频像素分析 CLI 工具。"""

__all__ = ["explain", "generate"]


def __getattr__(name: str):
    if name not in __all__:
        raise AttributeError(name)
    from pixelprobe import api

    value = getattr(api, name)
    globals()[name] = value
    return value
