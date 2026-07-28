"""生成 PixelProbe MCP 的确定性只读评估素材。"""

from __future__ import annotations

import sys
from pathlib import Path

from generate_test_image import generate_test_image
from generate_test_video import generate_test_video


def main() -> None:
    root = (
        Path(sys.argv[1]) if len(sys.argv) > 1
        else Path("evaluations") / "fixtures"
    )
    root.mkdir(parents=True, exist_ok=True)
    image = generate_test_image(root / "精确图片.png")
    video = generate_test_video(root / "精确视频.mkv")
    print(f"已生成：{image}")
    print(f"已生成：{video}")


if __name__ == "__main__":
    main()
