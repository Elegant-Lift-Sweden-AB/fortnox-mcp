"""Optional access restrictions, defined by the company running the server.

Without FORTNOX_ROLE_FILE the server offers every tool and hides nothing. To give someone a narrower view, write a
role file (JSON) and start a separate server for them with FORTNOX_ROLE_FILE=/path/to/role.json. The server then
enforces it, not the AI:

  1. tools not listed in "tools" are removed (not listed, not callable);
  2. "params" refuses arguments that would cross the boundary: "forbid" (any explicit value, incl. 0),
     "only" (allowed values) and "writable" (allowed keys in free-form dict/row parameters, nested;
     an unknown key is refused);
  3. every result is scrubbed recursively: keys in "hide_fields" (exact, case-sensitive) are removed, and CSV columns
     matching "hide_csv_columns" (a regex) are dropped;
  4. errors never echo Fortnox's text: they become "HTTP <status>, code <n>, reference <id>", and the full text is
     logged server-side under that reference.

examples/roles/ holds two example files (a sales and a sourcing split). They show the format; what each person may
do is your decision. A file the server can't fully understand refuses to start (fail closed).

Two things a role file cannot narrow, so leave them out of a restricted role unless that is intended:
  * api_request reaches any Fortnox endpoint with any body; "params" rules don't apply to it (answers are still
    scrubbed);
  * get_document_pdf returns the PDF as Fortnox prints it; a PDF can't be scrubbed, so it shows whatever the print
    template shows (use "only" to limit document_type).

Role file format:
  {
    "name": "sales",                          # shown in logs and errors
    "instructions": "text for the AI",        # optional, appended to the server instructions
    "tools": ["get_article", ...],            # required: the tools this role gets
    "hide_fields": ["PurchasePrice", ...],    # optional: result keys to remove
    "hide_csv_columns": "(?i)supplier|cost",  # optional: regex for CSV columns to remove (stock_warnings)
    "params": {                               # optional, per tool:
      "create_article": {
        "forbid": ["sales_price"],            #   parameters that may not be given
        "only": {"document_type": ["invoice"]},   # allowed values of a parameter
        "writable": {"extra_fields": ["Weight", "Note", {"Nested": ["A", "B"]}],
                     "rows": {"each": ["article_number", "quantity"]}}
      }
    }
  }
A "writable" spec is a list of allowed keys (a string = a plain value, {"Key": spec} = a nested object), or
{"each": spec} for a list of objects.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass, field
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

log = logging.getLogger("fortnox_mcp.roles")

CSV_TOOLS = {"stock_warnings"}  # tools whose result has CSV rows (column names are scrubbed with hide_csv_columns)
_FILE_KEYS = {"name", "instructions", "tools", "hide_fields", "hide_csv_columns", "params"}
_PARAM_KEYS = {"forbid", "only", "writable"}


@dataclass
class Role:
    name: str
    tools: set[str]
    instructions: str = ""
    hide_fields: list[str] = field(default_factory=list)
    hide_csv: re.Pattern | None = None
    params: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def hide_re(self) -> re.Pattern | None:
        if not self.hide_fields:
            return None
        return re.compile("^(" + "|".join(re.escape(n) for n in self.hide_fields) + ")$")


# ---------------------------------------------------------------- loading (fail closed)

def _spec(value: Any, where: str) -> Any:
    """JSON writable-spec -> internal spec: dict key -> None (scalar) | dict (object) | [spec] (list of objects)."""
    if isinstance(value, dict) and set(value) == {"each"}:
        return [_spec(value["each"], f"{where}.each")]
    if isinstance(value, list):
        out: dict[str, Any] = {}
        for item in value:
            if isinstance(item, str):
                out[item] = None
            elif isinstance(item, dict) and len(item) == 1:
                (k, v), = item.items()
                out[k] = _spec(v, f"{where}.{k}")
            else:
                raise SystemExit(f"role file: {where}: entries must be a key name or {{\"Key\": spec}}, got {item!r}")
        return out
    raise SystemExit(f"role file: {where}: expected a list of keys or {{\"each\": ...}}, got {value!r}")


def parse_role(doc: Any, source: str = "role file") -> Role:
    if not isinstance(doc, dict):
        raise SystemExit(f"{source}: must be a JSON object")
    unknown = set(doc) - _FILE_KEYS
    if unknown:
        raise SystemExit(f"{source}: unknown key(s) {sorted(unknown)}; allowed: {sorted(_FILE_KEYS)}")
    tools = doc.get("tools")
    if not isinstance(tools, list) or not tools or not all(isinstance(t, str) for t in tools):
        raise SystemExit(f"{source}: \"tools\" must be a non-empty list of tool names")
    hide = doc.get("hide_fields", [])
    if not isinstance(hide, list) or not all(isinstance(h, str) for h in hide):
        raise SystemExit(f"{source}: \"hide_fields\" must be a list of field names")
    csv_re = None
    if doc.get("hide_csv_columns"):
        try:
            csv_re = re.compile(doc["hide_csv_columns"])
        except (re.error, TypeError) as e:
            raise SystemExit(f"{source}: \"hide_csv_columns\" is not a valid regex: {e}")
    params: dict[str, dict[str, Any]] = {}
    for tool, rules in (doc.get("params") or {}).items():
        if tool not in tools:
            raise SystemExit(f"{source}: params for {tool!r}, which is not in \"tools\"")
        if not isinstance(rules, dict) or set(rules) - _PARAM_KEYS:
            raise SystemExit(f"{source}: params.{tool} may only contain {sorted(_PARAM_KEYS)}")
        forbid = rules.get("forbid", [])
        only = rules.get("only", {})
        if not isinstance(forbid, list) or not isinstance(only, dict) or not all(isinstance(v, list) for v in only.values()):
            raise SystemExit(f"{source}: params.{tool}: \"forbid\" is a list of parameter names, \"only\" maps a "
                             f"parameter to a list of allowed values")
        params[tool] = {
            "forbid": list(forbid),
            "only": {p: list(v) for p, v in only.items()},
            "writable": {p: _spec(s, f"params.{tool}.writable.{p}") for p, s in (rules.get("writable") or {}).items()},
        }
    name = str(doc.get("name") or "restricted")
    return Role(name=name, tools=set(tools), instructions=str(doc.get("instructions") or ""), hide_fields=hide,
                hide_csv=csv_re, params=params)


def load_role_from_env() -> Role | None:
    path = os.environ.get("FORTNOX_ROLE_FILE", "").strip()
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        raise SystemExit(f"FORTNOX_ROLE_FILE={path!r} could not be read: {e}; refusing to start")
    return parse_role(doc, source=path)


# ---------------------------------------------------------------- result scrubbing

def scrub(obj: Any, role: Role | None, csv_rows: bool = False) -> Any:
    """Remove hidden keys recursively (dicts, lists, tuples). Scalars pass through."""
    if role is None:
        return obj
    pattern = role.hide_csv if csv_rows else role.hide_re
    if pattern is None:
        return obj
    if isinstance(obj, dict):
        return {k: scrub(v, role, csv_rows) for k, v in obj.items() if not (isinstance(k, str) and pattern.search(k))}
    if isinstance(obj, list):
        return [scrub(v, role, csv_rows) for v in obj]
    if isinstance(obj, tuple):
        return tuple(scrub(v, role, csv_rows) for v in obj)
    return obj


# ---------------------------------------------------------------- parameter rules

def _validate(value: Any, spec: Any, path: str, role: Role) -> None:
    if value is None:
        return
    if isinstance(spec, list):  # list of objects
        if not isinstance(value, list):
            raise ToolError(f"{path}: expected a list")
        for i, item in enumerate(value):
            _validate(item, spec[0], f"{path}[{i}]", role)
        return
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            raise ToolError(f"{path}: expected an object")
        bad = sorted(k for k in value if k not in spec)
        if bad:
            raise ToolError(f"{path}: field(s) {bad} are not writable for the {role.name} role. Allowed: {sorted(spec)}")
        for k, v in value.items():
            _validate(v, spec[k], f"{path}.{k}", role)
        return
    # spec is None: scalar only
    if isinstance(value, (dict, list, tuple)):
        raise ToolError(f"{path}: nested values are not allowed here for the {role.name} role")


def check_params(tool: str, role: Role, kwargs: dict[str, Any]) -> None:
    rules = role.params.get(tool)
    if not rules:
        return
    for p in rules["forbid"]:
        v = kwargs.get(p)
        if v is not None and v is not False:
            raise ToolError(f"{tool}: parameter {p!r} is not available for the {role.name} role (any explicit value, "
                            f"including 0, is refused). Ask your Fortnox administrator if you need it.")
    for p, allowed in rules["only"].items():
        if kwargs.get(p) not in allowed:
            raise ToolError(f"{tool}: {p}={kwargs.get(p)!r} is not available for the {role.name} role; "
                            f"allowed: {sorted(allowed, key=str)}. Ask your Fortnox administrator if you need more.")
    for p, spec in rules["writable"].items():
        _validate(kwargs.get(p), spec, f"{tool}.{p}", role)


# ---------------------------------------------------------------- errors: never echo upstream text

_FX_ERR = re.compile(r"Fortnox HTTP (\d{3})(?: code (\S+))?")
# the server's own, authored messages that are safe and useful to pass on (validation written in server.py)
_OWN_PREFIXES = ("Nothing to change", "Stock is never changed", "Each row needs", "Article number", "Description is empty",
                 "EAN ", "Price must be", "No price on price list", "webshop_article=true needs", "This invoice has been sent",
                 "Invoice is not booked", "Could not read the article's current supplier entries", "Customer ",
                 "Supplier ", "A customer with", "A supplier with")


def role_error(exc: BaseException, tool: str, role: Role) -> ToolError:
    ref = secrets.token_hex(4)
    text = str(exc)
    m = _FX_ERR.search(text)
    log.warning("fortnox_mcp ref=%s role=%s tool=%s error=%s: %s", ref, role.name, tool, type(exc).__name__, text)
    if m:
        code = f", code {m.group(2)}" if m.group(2) else ""
        return ToolError(f"Fortnox refused the request (HTTP {m.group(1)}{code}). Nothing was changed by this call "
                         f"unless the answer above says otherwise. Server reference {ref}; ask your administrator if it blocks you.")
    if isinstance(exc, ToolError) and (text.startswith(_OWN_PREFIXES) or text.startswith(f"{tool}")):
        return ToolError(scrub_text(text, role))
    return ToolError(f"{tool} failed on the server (reference {ref}). Nothing was sent. Ask your administrator to check the log.")


def scrub_text(text: str, role: Role) -> str:
    """Belt and braces for our own messages: redact values of hidden keys, any casing, JSON or key=value form."""
    if not role.hide_fields:
        return text
    alt = "|".join(re.escape(n) for n in role.hide_fields)
    return re.sub(rf'(["\']?)\b({alt})\b\1(\s*[:=]\s*)("[^"]*"|\'[^\']*\'|[^,}}\]\s]+)', r'\1\2\1\3"[hidden]"',
                  text, flags=re.I)


# ---------------------------------------------------------------- apply to a FastMCP server

def apply_role(mcp: FastMCP, role: Role | None) -> dict[str, Any]:
    """Remove the tools the role doesn't list and wrap the rest. Returns a summary."""
    manager = mcp._tool_manager
    names = sorted(manager._tools)
    if role is None:
        return {"role": "full access", "tools": len(names), "removed": []}
    missing = sorted(role.tools - set(names))
    if missing:
        raise SystemExit(f"role {role.name}: unknown tool(s) {missing}; refusing to start")
    removed = [n for n in names if n not in role.tools]
    for n in removed:
        manager.remove_tool(n)
    for n in sorted(manager._tools):
        tool = manager._tools[n]
        props = tool.parameters.get("properties", {})
        for p in [*role.params.get(n, {}).get("forbid", []), *role.params.get(n, {}).get("only", {}),
                  *role.params.get(n, {}).get("writable", {})]:
            if p not in props:
                raise SystemExit(f"role {role.name}: params.{n} names {p!r}, which {n} does not have; refusing to start")
        tool.fn = _wrap(tool.fn, n, role)
        tool.fn.__guarded_role__ = role.name  # type: ignore[attr-defined]
        # advertise the narrowed enum so clients never offer a value the role refuses
        for p, allowed in role.params.get(n, {}).get("only", {}).items():
            if isinstance(props.get(p), dict):
                props[p]["enum"] = sorted((v for v in allowed if v is not None), key=str)
    return {"role": role.name, "tools": len(manager._tools), "removed": removed}


def _wrap(fn, name: str, role: Role):
    @functools.wraps(fn)
    async def guarded(**kwargs):
        check_params(name, role, kwargs)
        try:
            result = await fn(**kwargs)
        except Exception as e:
            raise role_error(e, name, role) from None
        if name in CSV_TOOLS and isinstance(result, dict):
            result = dict(result)
            result["rows"] = scrub(result.get("rows", []), role, csv_rows=True)
        return scrub(result, role)
    guarded.__guarded_role__ = role.name  # type: ignore[attr-defined]
    return guarded
