"""源地址绑定准入检查。

背景：重部署补采服务时，若把 OAI_ENDPOINT 误指向演练源却沿用正式库的
数据卷，服务会拿正式库的截止日续采，并按相同 OAI 标识覆盖书目。

规则：
1. 新库（无书目、无续采进度）：首次补采成功的同一事务内自动绑定当前
   OAI_ENDPOINT；采集失败（含源不可达）不绑定。
2. 旧库已有数据但未绑定：来源无法自动确认，默认拒绝采集；管理员须通过
   单独的配置项 CONFIRMED_OAI_ENDPOINT 填写核实过的原地址，且与当前
   OAI_ENDPOINT 完全相同才放行，并在本次成功采集的事务内补绑定。
3. 已绑定但当前地址与绑定地址不符：拒绝采集。

所有拒绝情况都抛 SourceBindingError（路由层映射为 409），
不访问 OAI 源，也不写任何运行历史；目录与进度仍可正常查询，
把 OAI_ENDPOINT 改回已绑定地址后即可照常续采。
"""
from __future__ import annotations

import os
import sqlite3

from . import db

# 单独的配置项（与日常使用的 OAI_ENDPOINT 分开）：
# 仅用于旧库未绑定时，由管理员显式填写核实过的原地址。
CONFIRMED_ENDPOINT_ENV = "CONFIRMED_OAI_ENDPOINT"


class SourceBindingError(RuntimeError):
    """源地址绑定检查未通过，路由层映射为 HTTP 409。"""


def get_confirmed_endpoint() -> str | None:
    value = os.environ.get(CONFIRMED_ENDPOINT_ENV, "").strip()
    return value or None


def preflight(conn: sqlite3.Connection, current_endpoint: str) -> None:
    """采集前的源地址准入检查。只读，不写库、不访问源。"""
    current = db.normalize_endpoint(current_endpoint)
    bound = db.get_bound_endpoint(conn)

    if bound is not None:
        if bound != current:
            raise SourceBindingError(
                "当前 OAI_ENDPOINT 与该数据卷已绑定的源地址不符，已拒绝采集："
                f"当前地址={current}；已绑定地址={bound}。"
                "为避免用其他来源（如演练源）覆盖正式库书目，本次不会访问源、"
                "也不记录运行历史。请把 OAI_ENDPOINT 改回已绑定地址后重试；"
                "原目录与续采进度仍可查询。"
            )
        return

    # 未绑定：只有空的新库允许自动绑定
    if not db.has_local_data(conn):
        return

    # 旧库有数据但未绑定：来源未确认
    confirmed_raw = get_confirmed_endpoint()
    if confirmed_raw is None:
        raise SourceBindingError(
            "该数据卷已有书目或续采进度，但尚未绑定 OAI 源地址，来源无法自动"
            "确认，已拒绝采集：当前地址=" + current + "。"
            "请通过单独的配置项 " + CONFIRMED_ENDPOINT_ENV
            + " 填写核实过的原地址（须与当前 OAI_ENDPOINT 完全相同）后重试。"
            "本次不会访问源、也不记录运行历史；原目录与续采进度仍可查询。"
        )

    confirmed = db.normalize_endpoint(confirmed_raw)
    if confirmed != current:
        raise SourceBindingError(
            "配置项 " + CONFIRMED_ENDPOINT_ENV + " 填写的核实地址与当前 "
            "OAI_ENDPOINT 不符，已拒绝采集：核实地址=" + confirmed
            + "；当前地址=" + current + "。只有两者完全相同才允许补绑定并采集，"
            "本次不会访问源、也不记录运行历史。"
        )
