"""Read-only marking: only tools that cannot change anything may carry readOnlyHint (clients run those in parallel)."""
from conftest import load_server

server = load_server("admin")


def _tools():
    return server.mcp._tool_manager._tools


def test_no_write_tool_is_marked_read_only():
    for name, tool in _tools().items():
        has_dry_run = "dry_run" in tool.parameters.get("properties", {})
        marked = bool(tool.annotations and tool.annotations.readOnlyHint)
        assert not (has_dry_run and marked), f"{name} is a write tool (dry_run) but is marked read-only"
        assert marked == (name in server.READ_ONLY_TOOLS), name


def test_every_unmarked_tool_without_dry_run_is_a_known_exception():
    unmarked = {n for n, t in _tools().items()
                if "dry_run" not in t.parameters.get("properties", {}) and n not in server.READ_ONLY_TOOLS}
    assert unmarked == set(), f"new tool without dry_run and without a read-only decision: {unmarked}"


async def test_hint_is_published_in_the_tool_list():
    listed = {t.name: t for t in await server.mcp.list_tools()}  # a staff role removes some tools
    assert listed["get_article"].annotations.readOnlyHint is True
    assert listed["get_document_pdf"].annotations.readOnlyHint is True
    for name in ("set_price", "api_request", "create_article"):
        if name in listed:
            assert listed[name].annotations is None or not listed[name].annotations.readOnlyHint, name
