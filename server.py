#!/usr/bin/env python3
"""上下班打卡 PWA 服务端（数据持久化到 SQLite）

用法:
    python server.py -p 9000              # 指定端口（短参数）
    python server.py --port 9000         # 指定端口（长参数）

注意：端口必须通过 -p/--port 显式指定，不支持位置参数（python server.py 9000），
以免随手写数字被误当成其他含义而导致端口不符预期。

数据存储：
    打卡记录与节假日缓存均存放在项目根目录的 data.db（SQLite），
    不再依赖浏览器 localStorage。
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional, Tuple

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data.db"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 中国节假日 API（timor.tech 免费接口，支持法定节假日与调休）
HOLIDAY_API_URL = "https://timor.tech/api/holiday/year/{year}/"


def _valid_date(s: str) -> bool:
    if not DATE_RE.match(s or ""):
        return False
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    conn = get_db()
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS punch_records (
                date TEXT PRIMARY KEY,
                on_time TEXT,
                off_time TEXT,
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS holiday_cache (
                year INTEGER PRIMARY KEY,
                data TEXT NOT NULL,
                fetched_at TEXT DEFAULT (datetime('now','localtime'))
            )"""
        )
        conn.commit()
    finally:
        conn.close()


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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(json.dumps(body, ensure_ascii=False).encode("utf-8"))

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_OPTIONS(self):
        self._send_json(200, {})

    # ---------------- 打卡记录 ----------------
    def _list_records(self) -> dict:
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT date, on_time, off_time FROM punch_records"
            ).fetchall()
        finally:
            conn.close()
        return {r["date"]: {"on": r["on_time"], "off": r["off_time"]} for r in rows}

    def _upsert_record(self, date: str, on_time, off_time):
        conn = get_db()
        try:
            if not on_time and not off_time:
                conn.execute("DELETE FROM punch_records WHERE date=?", (date,))
            else:
                conn.execute(
                    "INSERT INTO punch_records(date, on_time, off_time, updated_at) "
                    "VALUES(?,?,?,datetime('now','localtime')) "
                    "ON CONFLICT(date) DO UPDATE SET "
                    "on_time=excluded.on_time, off_time=excluded.off_time, "
                    "updated_at=datetime('now','localtime')",
                    (date, on_time, off_time),
                )
            conn.commit()
        finally:
            conn.close()

    def _delete_record(self, date: str):
        conn = get_db()
        try:
            conn.execute("DELETE FROM punch_records WHERE date=?", (date,))
            conn.commit()
        finally:
            conn.close()

    def _delete_all_records(self):
        conn = get_db()
        try:
            conn.execute("DELETE FROM punch_records")
            conn.commit()
        finally:
            conn.close()

    def _import_records(self, data: dict):
        conn = get_db()
        try:
            cur = conn.cursor()
            for date, rec in data.items():
                if not _valid_date(date):
                    continue
                if isinstance(rec, dict):
                    on_time = rec.get("on") or None
                    off_time = rec.get("off") or None
                else:
                    on_time = off_time = None
                if not on_time and not off_time:
                    cur.execute("DELETE FROM punch_records WHERE date=?", (date,))
                else:
                    cur.execute(
                        "INSERT INTO punch_records(date, on_time, off_time, updated_at) "
                        "VALUES(?,?,?,datetime('now','localtime')) "
                        "ON CONFLICT(date) DO UPDATE SET "
                        "on_time=excluded.on_time, off_time=excluded.off_time, "
                        "updated_at=datetime('now','localtime')",
                        (date, on_time, off_time),
                    )
            conn.commit()
        finally:
            conn.close()

    # ---------------- 节假日缓存 ----------------
    def _get_holiday_cache(self, year: int):
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT data FROM holiday_cache WHERE year=?", (year,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        try:
            return json.loads(row["data"])
        except json.JSONDecodeError:
            return None

    def _set_holiday_cache(self, year: int, data: dict):
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO holiday_cache(year, data, fetched_at) "
                "VALUES(?,?,datetime('now','localtime')) "
                "ON CONFLICT(year) DO UPDATE SET "
                "data=excluded.data, fetched_at=datetime('now','localtime')",
                (year, json.dumps(data, ensure_ascii=False)),
            )
            conn.commit()
        finally:
            conn.close()

    # ---------------- 路由 ----------------
    def do_GET(self):
        if self.path == "/api/records":
            try:
                return self._send_json(200, self._list_records())
            except Exception as e:
                return self._send_json(500, {"error": str(e)})

        if self.path.startswith("/api/holidays/"):
            try:
                parts = self.path.split("?", 1)
                path_part = parts[0].rstrip("/")
                query = urllib.parse.parse_qs(parts[1]) if len(parts) > 1 else {}
                refresh = query.get("refresh", ["0"])[0] == "1"
                year = int(path_part.split("/")[-1])
                if year < 2000 or year > 2100:
                    raise ValueError("年份超出范围")
                cached = self._get_holiday_cache(year)
                if cached is not None and not refresh:
                    return self._send_json(
                        200, {"year": year, "data": cached, "cached": True}
                    )
                data = fetch_holidays(year)
                self._set_holiday_cache(year, data)
                return self._send_json(200, {"year": year, "data": data})
            except Exception as e:
                return self._send_json(500, {"error": str(e)})

        return super().do_GET()

    def do_PUT(self):
        if self.path.startswith("/api/records/"):
            try:
                date = urllib.parse.unquote(self.path[len("/api/records/"):].split("?", 1)[0])
                if not _valid_date(date):
                    return self._send_json(400, {"error": "日期格式应为 YYYY-MM-DD"})
                body = self._read_json()
                on_time = body.get("on") or None
                off_time = body.get("off") or None
                self._upsert_record(date, on_time, off_time)
                return self._send_json(200, {"ok": True, "date": date})
            except Exception as e:
                return self._send_json(500, {"error": str(e)})
        return self._send_json(404, {"error": "Not Found"})

    def do_DELETE(self):
        if self.path == "/api/records":
            try:
                self._delete_all_records()
                return self._send_json(200, {"ok": True})
            except Exception as e:
                return self._send_json(500, {"error": str(e)})
        if self.path.startswith("/api/records/"):
            try:
                date = urllib.parse.unquote(self.path[len("/api/records/"):].split("?", 1)[0])
                if not _valid_date(date):
                    return self._send_json(400, {"error": "日期格式应为 YYYY-MM-DD"})
                self._delete_record(date)
                return self._send_json(200, {"ok": True, "date": date})
            except Exception as e:
                return self._send_json(500, {"error": str(e)})
        return self._send_json(404, {"error": "Not Found"})

    def do_POST(self):
        if self.path == "/api/records/import":
            try:
                data = self._read_json()
                if not isinstance(data, dict):
                    return self._send_json(400, {"error": "导入数据应为对象"})
                self._import_records(data)
                return self._send_json(200, {"ok": True, "count": len(data)})
            except Exception as e:
                return self._send_json(500, {"error": str(e)})
        return self._send_json(404, {"error": "Not Found"})


def get_port() -> int:
    parser = argparse.ArgumentParser(
        description="上下班打卡 PWA 服务端",
        usage="python server.py -p PORT  (或 --port PORT)",
    )
    parser.add_argument(
        "-p",
        "--port",
        dest="port",
        type=int,
        required=True,
        help="监听端口（必填，例如 -p 9000）",
    )
    args = parser.parse_args()
    if args.port < 1 or args.port > 65535:
        parser.error("端口号必须在 1-65535 之间")
    return args.port


def main():
    init_db()
    port = get_port()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"打卡应用已启动: http://localhost:{port}")
    print(f"数据库: {DB_PATH}")
    print("按 Ctrl+C 停止服务")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服务...")
        server.shutdown()


if __name__ == "__main__":
    main()
