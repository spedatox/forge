"""Codebase-understanding tools, backed by the Graphify sidecar (§5).

These let the agent query a knowledge graph of the codebase instead of re-reading
whole files — the mechanism that reduces reliance on aggressive context compaction
(§5). All three are read-only and concurrency-safe (they only read the graph). If
the graph is unavailable, they return an is_error result telling the model to fall
back to reading files (§4)."""
from __future__ import annotations

from pydantic import BaseModel, Field

from forge.warden.tool import Tool, ToolContext, ToolResult


def _no_graph(ctx: ToolContext) -> ToolResult | None:
    if ctx.graph is None or not getattr(ctx.graph, "available", False):
        reason = getattr(ctx.graph, "unavailable_reason", "no graph indexed for this session")
        return ToolResult(
            f"The codebase graph is unavailable ({reason}). Fall back to reading files "
            f"directly with read_file.", is_error=True)
    return None


class GraphQueryArgs(BaseModel):
    question: str = Field(description="Natural-language question or keyword to search the graph for.")
    mode: str = Field(default="bfs", description="Traversal: 'bfs' (broad) or 'dfs' (deep).")
    depth: int = Field(default=3, description="How many hops to expand from matched nodes.")
    context_filter: list[str] | None = Field(
        default=None,
        description=(
            "Restrict to edge kinds, e.g. ['call'] or ['call','import']. The sharpest "
            "way to cut noise when a broad question drags in vendored bundles and "
            "unrelated tests — ask for the relationship you actually mean."))
    token_budget: int = Field(
        default=2000,
        description=(
            "How much of the result to return. Raise it when the answer says it "
            "truncated and the node you needed may be among the ones cut; a truncated "
            "answer that hid the match is worse than a larger one."))


class GraphQuery(Tool):
    name = "graph_query"
    description = (
        "Query a knowledge graph of the codebase with a natural-language question and "
        "get back the relevant functions, classes, files, and how they connect — as "
        "compact text context. Use this FIRST to orient yourself instead of reading many "
        "files: 'what calls the auth handler?', 'where is retry logic defined?'. It is "
        "read-only and safe to run in parallel. Returns graph context, not file contents "
        "— follow up with read_file on the specific files it points you to."
    )
    Args = GraphQueryArgs
    READ_ONLY = True
    CONCURRENCY_SAFE = True

    async def call(self, args: GraphQueryArgs, ctx: ToolContext) -> ToolResult:
        if (na := _no_graph(ctx)):
            return na
        try:
            payload = {"question": args.question, "mode": args.mode,
                       "depth": args.depth, "token_budget": args.token_budget}
            if args.context_filter:
                payload["context_filter"] = args.context_filter
            text = await ctx.graph.call("query_graph", payload)
        except Exception as e:  # noqa: BLE001
            return ToolResult(f"graph query failed: {e}", is_error=True)
        return ToolResult(text or "(no matching nodes in the graph)")


class GraphPathArgs(BaseModel):
    source: str = Field(description="Label of the start node (e.g. a function or class name).")
    target: str = Field(description="Label of the end node.")
    undirected: bool = Field(
        default=False,
        description=(
            "Ignore edge direction. Set this when a directed search finds nothing — "
            "the graph is asymmetric, so A→B can be missing while B→A exists, and "
            "'how are these two related' is usually an undirected question."))
    max_hops: int = Field(
        default=6, description="How far to search before giving up.")


class GraphPath(Tool):
    name = "graph_path"
    description = (
        "Find the shortest relationship path between two named entities in the codebase "
        "graph — e.g. how a route handler is connected to a database model. Use it to "
        "understand indirect coupling before making a change. It is read-only and "
        "parallel-safe. Returns the chain of nodes and the edges linking them, or a note "
        "that no path exists."
    )
    Args = GraphPathArgs
    READ_ONLY = True
    CONCURRENCY_SAFE = True

    async def call(self, args: GraphPathArgs, ctx: ToolContext) -> ToolResult:
        if (na := _no_graph(ctx)):
            return na
        try:
            text = await ctx.graph.call("shortest_path", {
                "source": args.source, "target": args.target,
                "undirected": args.undirected, "max_hops": args.max_hops})
        except Exception as e:  # noqa: BLE001
            return ToolResult(f"graph path lookup failed: {e}", is_error=True)
        return ToolResult(text or "(no path found)")


class GraphOverviewArgs(BaseModel):
    top_n: int = Field(default=10, description="How many of the most-connected nodes to list.")


class GraphOverview(Tool):
    name = "graph_overview"
    description = (
        "Get a high-level map of the codebase: overall graph statistics plus the "
        "'god nodes' — the most-connected functions/classes, which are usually the core "
        "abstractions worth understanding first. Use this at the start of a task on an "
        "unfamiliar codebase. It is read-only and parallel-safe. Returns counts and a "
        "ranked list of central entities."
    )
    Args = GraphOverviewArgs
    READ_ONLY = True
    CONCURRENCY_SAFE = True

    async def call(self, args: GraphOverviewArgs, ctx: ToolContext) -> ToolResult:
        if (na := _no_graph(ctx)):
            return na
        try:
            stats = await ctx.graph.call("graph_stats", {})
            gods = await ctx.graph.call("god_nodes", {"top_n": args.top_n})
        except Exception as e:  # noqa: BLE001
            return ToolResult(f"graph overview failed: {e}", is_error=True)
        return ToolResult(f"{stats}\n{gods}")


class GraphIndexArgs(BaseModel):
    path: str = Field(
        default=".",
        description="Directory to index, relative to the workspace. '.' is the workspace root.")
    refresh: bool = Field(
        default=False,
        description=(
            "Re-index from scratch. Use after you have changed a lot of code and the "
            "graph's answers have gone stale; leave false to reuse an existing index, "
            "which is far faster."))


class GraphIndex(Tool):
    name = "graph_index"
    description = (
        "Builds (or rebuilds) the codebase graph for a directory, so the other graph "
        "tools can answer questions about it. Use it when you start work on a repo that "
        "has no graph yet — graph_query and the rest will tell you when there is none — "
        "or with refresh=true after making enough changes that the graph has gone stale. "
        "Indexing reads the whole tree and takes anywhere from seconds to a few minutes "
        "on a large repo, so do NOT call it speculatively or once per edit; one index at "
        "the start of real work on a codebase is the normal pattern. It is local and "
        "costs nothing — the code is parsed on this machine and nothing is sent "
        "anywhere. Returns the graph's size and what it now covers."
    )
    Args = GraphIndexArgs

    # Indexing writes graphify-out/ into the tree, and two indexes racing over
    # the same directory would fight over that file.
    READ_ONLY = False
    CONCURRENCY_SAFE = False
    DESTRUCTIVE = False

    async def call(self, args: GraphIndexArgs, ctx: ToolContext) -> ToolResult:
        from pathlib import Path

        from forge.graph.sidecar import GraphSidecar

        root = getattr(getattr(ctx, "cell", None), "host_path", None)
        if root is None:
            return ToolResult("No workspace to index.", is_error=True)

        root = Path(root).resolve()
        target = (root / (args.path or ".")).resolve()
        # Same boundary the file tools enforce: an index is a full read of the
        # tree, so a path escaping the workspace would read the whole machine.
        if target != root and root not in target.parents:
            return ToolResult(
                f"Refused: {args.path} is outside the workspace.", is_error=True)
        if not target.is_dir():
            return ToolResult(f"Not a directory: {args.path}", is_error=True)

        if args.refresh:
            existing = target / "graphify-out" / "graph.json"
            try:
                existing.unlink(missing_ok=True)
            except OSError as e:
                return ToolResult(f"Could not clear the old index: {e}", is_error=True)

        # Replace whatever was indexed before — one graph per context, pointed
        # at whatever the agent is actually working on.
        previous = getattr(ctx, "graph", None)
        if previous is not None:
            try:
                await previous.close()
            except Exception:  # noqa: BLE001 — a stuck old sidecar must not block a new one
                pass

        sidecar = GraphSidecar(target)
        if not await sidecar.start():
            ctx.graph = None
            return ToolResult(
                f"Could not index {args.path}: {sidecar.unavailable_reason}. "
                "Work from read_file and grep instead.", is_error=True)

        ctx.graph = sidecar
        try:
            stats = await sidecar.call("graph_stats", {})
        except Exception:  # noqa: BLE001 — indexed is the result; stats are a bonus
            stats = ""
        return ToolResult(
            f"Indexed {args.path}. The graph tools can now answer questions about it.\n"
            f"{stats}".strip())
