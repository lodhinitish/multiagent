"""Example tools. Import this module (or your own) to register tools before
starting the API. Add your own the same way with `@tool`."""
from __future__ import annotations

import ast
import operator
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from . import loan_tools  # noqa: F401  registers get_loans_by_phone, get_emi_details, create_loan
from .tools import ToolError, tool

_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv, ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ToolError(f"Unsupported expression element: {ast.dump(node)}")


@tool
def calculator(expression: str) -> str:
    """Evaluate an arithmetic expression, e.g. '(2 + 3) * 4 ** 2'. Supports + - * / // % **."""
    try:
        return str(_eval(ast.parse(expression, mode="eval").body))
    except (SyntaxError, ZeroDivisionError) as e:
        raise ToolError(str(e)) from e


@tool
def current_time(timezone: str = "UTC") -> str:
    """Current date and time in an IANA timezone such as 'Asia/Kolkata'."""
    try:
        return datetime.now(ZoneInfo(timezone)).isoformat()
    except Exception as e:
        raise ToolError(f"Bad timezone '{timezone}'") from e


@tool(timeout=20)
async def http_get(url: str, max_chars: int = 5000) -> str:
    """Fetch a URL over HTTP(S) and return the response body (truncated)."""
    if not url.startswith(("http://", "https://")):
        raise ToolError("URL must start with http:// or https://")
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        r = await client.get(url)
    return f"HTTP {r.status_code}\n{r.text[:max_chars]}"
