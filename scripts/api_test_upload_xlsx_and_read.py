"""Interactive acceptance test: upload tests/files/2.2.xlsx and have Pi read it.

The script uses the same login, Workspace, and Session selection flow as
``api_test_login_session_chat.py``. It derives sheet names and one non-empty
cell from each sheet locally, then requires the Agent to return the uploaded
file's SHA-256 and the sheet names. This makes a generic acknowledgement
insufficient to pass while keeping cell rendering available for human review.
"""
from __future__ import annotations

import json
import sys
from hashlib import sha256
from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree as ET

import httpx

from api_test_login_session_chat import (
    BASE_URL,
    create_session,
    select_session,
    select_workspace,
    sessions_for_workspace,
    show,
    show_history,
)
from getpass import getpass


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILE = REPOSITORY_ROOT / "tests" / "files" / "2.2.xlsx"
WORKSPACE_PATH = "tests/files/2.2.xlsx"
MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = {"m": MAIN_NS, "r": REL_NS}


def workbook_expectations(path: Path) -> list[tuple[str, str, str]]:
    """Return one visible non-empty cell per sheet using only the standard library."""
    with ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {entry.attrib["Id"]: entry.attrib["Target"] for entry in relationships}
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared_strings = ["".join(item.itertext()).strip() for item in shared.findall("m:si", NS)]

        expectations: list[tuple[str, str, str]] = []
        for sheet in workbook.findall("m:sheets/m:sheet", NS):
            relation = sheet.attrib[f"{{{REL_NS}}}id"]
            target = targets[relation].lstrip("/")
            sheet_path = target if target.startswith("xl/") else f"xl/{target}"
            root = ET.fromstring(archive.read(sheet_path))
            for cell in root.findall(".//m:c", NS):
                value = cell_value(cell, shared_strings)
                if value:
                    expectations.append((sheet.attrib["name"], cell.attrib["r"], value))
                    break
            else:
                raise RuntimeError(f"工作表 {sheet.attrib['name']} 没有可验证的非空单元格")
    if not expectations:
        raise RuntimeError("工作簿没有可验证的工作表")
    return expectations


def cell_value(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    value = cell.find("m:v", NS)
    if cell_type == "s" and value is not None:
        return shared_strings[int(value.text)].strip()
    if cell_type == "inlineStr":
        inline = cell.find("m:is", NS)
        return "".join(inline.itertext()).strip() if inline is not None else ""
    return value.text.strip() if value is not None and value.text else ""


def upload(client: httpx.Client, headers: dict[str, str], workspace_id: str) -> None:
    with SOURCE_FILE.open("rb") as source:
        response = client.post(
            f"{BASE_URL}/api/v1/workspaces/{workspace_id}/files",
            headers=headers,
            data={"path": WORKSPACE_PATH, "overwrite": "true"},
            files={"file": (SOURCE_FILE.name, source, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
    body = show("上传 2.2.xlsx", response)
    if body.get("path") != WORKSPACE_PATH or body.get("size_bytes") != SOURCE_FILE.stat().st_size:
        raise RuntimeError(f"上传返回与源文件不一致: {body}")


def read_task(expectations: list[tuple[str, str, str]]) -> str:
    instructions = "\n".join(f"- 工作表 {sheet}：报告单元格 {cell} 的值" for sheet, cell, _ in expectations)
    return f"""请只读取当前 Workspace 中的 `{WORKSPACE_PATH}`，验证这是一个可读取的 XLSX 工作簿。
使用 bash 和 Python 3 标准库（例如 zipfile 与 xml.etree）解析，不要访问网络或猜测内容。
请列出工作表名称，并逐项报告下列单元格的实际值：
{instructions}
最后使用以下机器可验证格式输出（不要省略任一行）：
`WORKBOOK_SHA256: <该文件的 sha256sum>`
`SHEET: <每个工作表名称>`（每个工作表各一行）
`WORKBOOK_READ_OK`。"""


def stream_and_capture(client: httpx.Client, headers: dict[str, str], session_id: str, task: str) -> str:
    print("\n[智能体流式返回]")
    done = False
    text: list[str] = []
    timeout = httpx.Timeout(connect=10.0, read=None, write=60.0, pool=60.0)
    with client.stream(
        "POST",
        f"{BASE_URL}/api/v1/sessions/{session_id}/messages:stream",
        headers=headers,
        json={"content": task},
        timeout=timeout,
    ) as response:
        if response.status_code == 409 and response.json().get("detail") == "session_busy":
            raise RuntimeError("该 Session 仍在运行；请等待结束或先 abort 后再测试。")
        response.raise_for_status()
        event_name: str | None = None
        for line in response.iter_lines():
            if line.startswith("event: "):
                event_name = line.removeprefix("event: ")
            elif line.startswith("data: ") and event_name:
                payload = json.loads(line.removeprefix("data: "))
                if event_name == "assistant.delta":
                    delta = payload.get("delta", "")
                    text.append(delta)
                    print(delta, end="", flush=True)
                elif event_name in {"tool.started", "tool.completed"}:
                    print(f"\n[{event_name}] {json.dumps(payload, ensure_ascii=False)}", flush=True)
                elif event_name == "message.failed":
                    raise RuntimeError(f"Agent 读取失败: {payload}")
                elif event_name == "done":
                    done = True
                event_name = None
    print()
    if not done:
        raise RuntimeError("SSE 在 done 事件前结束")
    return "".join(text)


def assert_workbook_read(response: str, expectations: list[tuple[str, str, str]], expected_sha256: str) -> None:
    # A matching digest proves the Agent accessed this exact uploaded binary.
    # Sheet names additionally prove it parsed workbook structure, while the
    # requested cells remain visible in the transcript for human review.
    missing = [f"SHEET: {sheet}" for sheet, _, _ in expectations if sheet not in response]
    if expected_sha256 not in response.lower():
        missing.append("WORKBOOK_SHA256: <matching sha256sum>")
    if "WORKBOOK_READ_OK" not in response:
        missing.append("WORKBOOK_READ_OK")
    if missing:
        raise RuntimeError("Agent 响应未证明已读取工作簿；缺少: " + "; ".join(missing))


def main() -> None:
    if not SOURCE_FILE.is_file():
        raise SystemExit(f"测试文件不存在: {SOURCE_FILE}")
    expectations = workbook_expectations(SOURCE_FILE)
    expected_sha256 = sha256(SOURCE_FILE.read_bytes()).hexdigest()
    print(f"Gateway: {BASE_URL}")
    print(f"测试文件: {SOURCE_FILE} ({SOURCE_FILE.stat().st_size} bytes)")
    email = input("登录邮箱: ").strip().lower()
    password = getpass("登录密码: ")
    with httpx.Client(timeout=60, trust_env=False) as client:
        login = show("用户登录", client.post(f"{BASE_URL}/api/v1/auth/login", json={"email": email, "password": password}))
        headers = {"Authorization": f"Bearer {login['access_token']}"}
        workspace = select_workspace(client, headers)
        upload(client, headers, workspace["id"])
        session = select_session(sessions_for_workspace(client, headers, workspace))
        if session is None:
            session = create_session(client, headers, workspace)
        show_history(client, headers, session["id"])
        response = stream_and_capture(client, headers, session["id"], read_task(expectations))
        assert_workbook_read(response, expectations, expected_sha256)
        show_history(client, headers, session["id"])
        print(f"\nPASS: Pi Agent 已读取 {WORKSPACE_PATH}。Session ID: {session['id']}")


if __name__ == "__main__":
    try:
        main()
    except (httpx.HTTPError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"\n测试失败: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
