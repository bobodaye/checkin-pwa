#!/usr/bin/env python3
"""上下班打卡 PWA 服务端

用法:
    python server.py              # 默认监听 9000 端口
    python server.py 8080         # 指定端口
    python server.py --port 8080  # 指定端口（长参数）
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Optional, Tuple

ROOT = Path(__file__).resolve().parent
DEFAULT_PORT = 9000

# 中国节假日 API（timor.tech 免费接口，支持法定节假日与调休）
HOLIDAY_API_URL = "https://timor.tech/api/holiday/year/{year}/"


def fetch_holidays(year: int) -> dict:
    """从 timor.tech 获取指定年份的中国节假日数据，并转换为统一格式。

    返回格式：
    {
      "2026-01-01": {"type": "holiday", "name": "元旦"},
      "2026-02-13": {"type": "rest", "name": ""},
      "2026-02-14": {"type": "workday", "name": "春节调休"},
      ...
    }
    type 可选值：
      holiday（标准法定节假日当天）
      rest（因节假日安排而放假/休息的日期，包括周末连休）
      workday（调休补班/上班）
    """
    url = HOLIDAY_API_URL.format(year=year)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        raise RuntimeError(f"获取节假日数据失败: {e}")

    holidays = data.get("holiday") or data.get("holidays") or {}
    result = {}

    def normalize_date(date_str: str) -> str:
        """统一返回 YYYY-MM-DD 格式。"""
        if len(date_str) == 10 and date_str.count("-") == 2:
            return date_str
        if len(date_str) == 5 and date_str.count("-") == 1:
            return f"{year}-{date_str}"
        return f"{year}-{date_str.replace('/', '-').replace('.', '-')}"

    def _classify(item: dict) -> Optional[Tuple[str, str]]:
        """根据 item 返回 (type, name) 或 None（不识别时）。

        timor.tech 字段说明：
          - wage=3 法定节假日（当天 3 倍工资）
          - wage=2 调休/连休的休息日
          - wage=1 调休补班日
        """
        name = item.get("name", "")
        holiday_flag = item.get("holiday")
        wage = item.get("wage")
        code = item.get("code")

        # 1. 优先依据 wage 判断（最准确）
        if isinstance(wage, int):
            if wage == 3:
                return ("holiday", name)
            if wage == 2:
                return ("rest", "")
            if wage == 1:
                return ("workday", "补班")

        # 2. 依据 code 判断（兼容其他 API 格式）
        if isinstance(code, int):
            if code == 2:
                return ("holiday", name)
            if code == 1:
                return ("rest", "")
            if code in (0, 3):
                return ("workday", "补班")

        # 3. 兜底按 holiday 标志 + 名称修饰词
        if isinstance(holiday_flag, bool):
            if holiday_flag:
                return ("rest", "")
            if "班" in name or "调休" in name or "补班" in name:
                return ("workday", "补班")
            return None

        # 4. 兜底按 name 判断
        if "班" in name or "调休" in name or "补班" in name:
            return ("workday", "补班")
        if "休" in name or "放假" in name or "假" in name:
            return ("rest", "")
        # 不含修饰词的非空名称视为法定节假日名称
        if name:
            return ("holiday", name)
        return None

    # timor.tech 常见格式：holiday 是 dict，key 为日期
    if isinstance(holidays, dict):
        for date_str, item in holidays.items():
            if not item:
                continue
            classified = _classify(item)
            if classified:
                t, n = classified
                result[normalize_date(date_str)] = {"type": t, "name": n}

    # 另一种常见格式：holiday 是 list
    elif isinstance(holidays, list):
        for item in holidays:
            if not isinstance(item, dict):
                continue
            date_str = item.get("date")
            if not date_str:
                continue
            classified = _classify(item)
            if classified:
                t, n = classified
                result[normalize_date(date_str)] = {"type": t, "name": n}

    # 后处理：连续同名 wage=3 节假日中，仅第一天保留 holiday 类型，其余降级为 rest。
    # 例如劳动节 5.1、5.2 同为 wage=3 且都叫“劳动节”，则只让 5.1 显示“劳动节”。
    sorted_dates = sorted(result.keys())
    final = {}
    i = 0
    while i < len(sorted_dates):
        d = sorted_dates[i]
        item = result[d]
        if item["type"] == "holiday":
            j = i + 1
            while j < len(sorted_dates):
                prev = datetime.strptime(sorted_dates[j - 1], "%Y-%m-%d").date()
                curr = datetime.strptime(sorted_dates[j], "%Y-%m-%d").date()
                if curr != prev + timedelta(days=1):
                    break
                nxt = result[sorted_dates[j]]
                if nxt["type"] != "holiday" or nxt["name"] != item["name"]:
                    break
                j += 1
            final[d] = item
            for k in range(i + 1, j):
                final[sorted_dates[k]] = {"type": "rest", "name": ""}
            i = j
        else:
            final[d] = item
            i += 1

    return final


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}")

    def _send_json(self, status: int, body: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(json.dumps(body, ensure_ascii=False).encode("utf-8"))

    def do_OPTIONS(self):
        self._send_json(200, {})

    def do_GET(self):
        if self.path.startswith("/api/holidays/"):
            try:
                year = int(self.path.split("/")[-1])
                if year < 2000 or year > 2100:
                    raise ValueError("年份超出范围")
                data = fetch_holidays(year)
                self._send_json(200, {"year": year, "data": data})
                return
            except Exception as e:
                self._send_json(500, {"error": str(e)})
                return
        return super().do_GET()


def get_port() -> int:
    parser = argparse.ArgumentParser(description="上下班打卡 PWA 服务端")
    parser.add_argument(
        "port",
        nargs="?",
        type=int,
        default=DEFAULT_PORT,
        help=f"监听端口（默认 {DEFAULT_PORT}）",
    )
    parser.add_argument(
        "--port",
        dest="port_opt",
        type=int,
        default=None,
        help=f"监听端口（默认 {DEFAULT_PORT}）",
    )
    args = parser.parse_args()
    return args.port_opt if args.port_opt is not None else args.port


def main():
    port = get_port()
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"打卡应用已启动: http://localhost:{port}")
    print("按 Ctrl+C 停止服务")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服务...")
        server.shutdown()


if __name__ == "__main__":
    main()
