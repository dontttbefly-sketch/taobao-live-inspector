#!/usr/bin/env python3
"""柱形图自定义颜色写法探测（2026-08-04）：飞书 VChart 裁剪版，
style.fill 不生效，一次推 3 种写法对照，看哪种能渲染出粉色。"""
import sys

sys.path.insert(0, ".")
from app.config import load_config
from app.notify.feishu import send_card

DATA = [
    {"time": "10:01", "指标": "成交金额", "值": 10},
    {"time": "10:02", "指标": "成交金额", "值": 20},
    {"time": "10:03", "指标": "成交金额", "值": 15},
    {"time": "10:04", "指标": "成交金额", "值": 30},
    {"time": "10:05", "指标": "成交金额", "值": 25},
]


def _chart(spec_extra: dict, label: str) -> dict:
    spec = {
        "type": "bar",
        "data": {"values": DATA},
        "xField": "time",
        "yField": "值",
        "axes": [{"orient": "left"}, {"orient": "bottom"}],
        "tooltip": {"visible": True},
    }
    spec.update(spec_extra)
    return {
        "tag": "chart",
        "chart_spec": spec,
        "height": "120px",
        "preview": True,
        "color_theme": "primary",
    }


def main() -> None:
    cfg = load_config()
    nf = (cfg.get("notify", {}) or {}).get("feishu", {}) or {}
    from app.notify.feishu import card_v2
    card = card_v2(
        "柱形颜色写法探测", "green",
        [
            {"tag": "markdown", "content": "**A：seriesField+color 数组**"},
            _chart({"seriesField": "指标", "color": ["#FF5E9C"]}, "A"),
            {"tag": "markdown", "content": "**B：顶层 color 字符串**"},
            _chart({"color": "#FF5E9C"}, "B"),
            {"tag": "markdown", "content": "**C：style.fill**"},
            _chart({"style": {"fill": "#FF5E9C"}}, "C"),
        ],
    )
    send_card(cfg, nf["chat_id"], card)
    print("已推送柱形颜色探测卡")


if __name__ == "__main__":
    main()
