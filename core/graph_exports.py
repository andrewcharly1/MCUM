"""Governed, deterministic graph exports with strict project and size budgets."""

from __future__ import annotations

import json
import re
from typing import Any

from .graph_analytics import _normalize_graph, _text


EXPORT_SCHEMA_VERSION = "mcum-graph-export-v1"
SENSITIVE_KEYS = ("password", "passwd", "secret", "token", "api_key", "apikey", "credential", "private_key")


def _clean_text(value: Any, limit: int = 500) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", _text(value))
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return "[truncated]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key in sorted(value, key=str):
            clean_key = _clean_text(key, 120)
            if any(secret in clean_key.lower() for secret in SENSITIVE_KEYS):
                result[clean_key] = "[redacted]"
            else:
                result[clean_key] = _sanitize(value[key], depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set)):
        return [_sanitize(item, depth=depth + 1) for item in list(value)[:100]]
    if isinstance(value, str):
        return _clean_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _clean_text(value)


def _public_node(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": node["project_id"],
        "id": node["id"],
        "canonical_key": _clean_text(node.get("canonical_key")),
        "title": _clean_text(node.get("title")),
        "qualified_name": _clean_text(node.get("qualified_name")),
        "entity_type": _clean_text(node.get("entity_type")),
        "relative_path": _clean_text(node.get("relative_path")),
        "signature": _clean_text(node.get("signature")),
        "confidence": node.get("confidence"),
        "metadata": _sanitize(node.get("metadata") or {}),
    }


def _public_edge(edge: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": edge["project_id"],
        "id": edge["id"],
        "source_id": edge["source_id"],
        "target_id": edge["target_id"],
        "relation_type": _clean_text(edge.get("relation_type"), 120),
        "confidence": edge.get("confidence"),
        "weight": edge.get("weight"),
        "metadata": _sanitize(edge.get("metadata") or {}),
    }


def build_graph_export(
    graph: dict[str, Any] | None = None,
    *,
    project_id: str,
    nodes: list[dict[str, Any]] | None = None,
    edges: list[dict[str, Any]] | None = None,
    snapshot_id: str | None = None,
    max_nodes: int = 500,
    max_edges: int = 1_000,
) -> dict[str, Any]:
    normalized_nodes, normalized_edges, source = _normalize_graph(
        graph, project_id=project_id, nodes=nodes, edges=edges
    )
    node_budget = max(0, int(max_nodes))
    edge_budget = max(0, int(max_edges))
    selected_nodes = normalized_nodes[:node_budget]
    selected_ids = {item["id"] for item in selected_nodes}
    eligible_edges = [
        item
        for item in normalized_edges
        if item["source_id"] in selected_ids and item["target_id"] in selected_ids
    ]
    selected_edges = eligible_edges[:edge_budget]
    truncated_nodes = max(0, len(normalized_nodes) - len(selected_nodes))
    truncated_edges = max(0, len(normalized_edges) - len(selected_edges))
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "project_id": source["project_id"],
        "snapshot_id": _text(snapshot_id) or source["snapshot_id"],
        "nodes": [_public_node(item) for item in selected_nodes],
        "edges": [_public_edge(item) for item in selected_edges],
        "budget": {
            "max_nodes": node_budget,
            "max_edges": edge_budget,
            "truncated": bool(truncated_nodes or truncated_edges),
            "truncated_nodes": truncated_nodes,
            "truncated_edges": truncated_edges,
            "unresolved_edges": source["unresolved_edges"],
        },
    }


def _json(payload: Any, *, indent: int | None = None) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=None if indent else (",", ":"), indent=indent)


def _fit_json_budget(payload: dict[str, Any], max_bytes: int | None, *, indent: int | None = None) -> str:
    if not max_bytes or max_bytes <= 0:
        return _json(payload, indent=indent)
    budget = int(max_bytes)
    while True:
        rendered = _json(payload, indent=indent)
        if len(rendered.encode("utf-8")) <= budget:
            return rendered
        payload["budget"]["truncated"] = True
        if payload["edges"]:
            payload["edges"].pop()
            payload["budget"]["truncated_edges"] += 1
            continue
        if payload["nodes"]:
            removed = payload["nodes"].pop()
            payload["budget"]["truncated_nodes"] += 1
            payload["edges"] = [
                edge
                for edge in payload["edges"]
                if edge["source_id"] != removed["id"] and edge["target_id"] != removed["id"]
            ]
            continue
        payload["budget"]["max_bytes_unattainable"] = True
        return rendered


def export_graph_json(
    graph: dict[str, Any] | None = None,
    *,
    project_id: str,
    snapshot_id: str | None = None,
    max_nodes: int = 500,
    max_edges: int = 1_000,
    max_bytes: int | None = None,
    indent: int | None = 2,
) -> str:
    payload = build_graph_export(
        graph,
        project_id=project_id,
        snapshot_id=snapshot_id,
        max_nodes=max_nodes,
        max_edges=max_edges,
    )
    return _fit_json_budget(payload, max_bytes, indent=indent)


def export_graph_ndjson(
    graph: dict[str, Any] | None = None,
    *,
    project_id: str,
    snapshot_id: str | None = None,
    max_nodes: int = 500,
    max_edges: int = 1_000,
    max_bytes: int | None = None,
) -> str:
    payload = build_graph_export(
        graph,
        project_id=project_id,
        snapshot_id=snapshot_id,
        max_nodes=max_nodes,
        max_edges=max_edges,
    )
    body_records = [
        *({"record_type": "node", **item} for item in payload["nodes"]),
        *({"record_type": "edge", **item} for item in payload["edges"]),
    ]
    body_lines = [_json(record) for record in body_records]
    manifest_record = {
        "record_type": "manifest",
        "schema_version": payload["schema_version"],
        "project_id": payload["project_id"],
        "snapshot_id": payload["snapshot_id"],
        "budget": payload["budget"],
    }
    if max_bytes and max_bytes > 0:
        preliminary = _json(manifest_record)
        full_size = len((preliminary + "\n").encode("utf-8")) + sum(
            len((line + "\n").encode("utf-8")) for line in body_lines
        )
        payload["budget"]["byte_truncated"] = full_size > int(max_bytes)
    manifest = _json(
        {
            **manifest_record,
            "budget": payload["budget"],
        }
    )
    lines = [manifest]
    size = len((manifest + "\n").encode("utf-8"))
    for line in body_lines:
        line_size = len((line + "\n").encode("utf-8"))
        if max_bytes and max_bytes > 0 and size + line_size > max_bytes:
            payload["budget"]["byte_truncated"] = True
            break
        lines.append(line)
        size += line_size
    return "\n".join(lines) + ("\n" if lines else "")


def _md(value: Any) -> str:
    return (
        _clean_text(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def export_wiki_markdown(
    graph: dict[str, Any] | None = None,
    *,
    project_id: str,
    snapshot_id: str | None = None,
    max_nodes: int = 300,
    max_edges: int = 600,
) -> str:
    payload = build_graph_export(
        graph,
        project_id=project_id,
        snapshot_id=snapshot_id,
        max_nodes=max_nodes,
        max_edges=max_edges,
    )
    lines = [
        f"# Graph Wiki: {_md(payload['project_id'])}",
        "",
        f"- Snapshot: `{_md(payload['snapshot_id'] or 'unspecified')}`",
        f"- Nodes: {len(payload['nodes'])}",
        f"- Edges: {len(payload['edges'])}",
        f"- Truncated: {'yes' if payload['budget']['truncated'] else 'no'}",
        "",
        "## Entities",
        "",
    ]
    for node in payload["nodes"]:
        locator = f" ({_md(node['relative_path'])})" if node["relative_path"] else ""
        lines.extend(
            [
                f"### {_md(node['title'] or node['id'])}",
                "",
                f"- ID: `{_md(node['id'])}`",
                f"- Type: `{_md(node['entity_type'])}`",
                f"- Location: `{_md(node['relative_path'] or 'n/a')}`",
                f"- Signature: `{_md(node['signature'] or 'n/a')}`",
                f"- Canonical: `{_md(node['canonical_key'])}`{locator}",
                "",
            ]
        )
    lines.extend(["## Relations", "", "| Source | Relation | Target | Confidence |", "|---|---|---|---:|"])
    for edge in payload["edges"]:
        lines.append(
            f"| `{_md(edge['source_id'])}` | {_md(edge['relation_type'])} | "
            f"`{_md(edge['target_id'])}` | {float(edge['confidence']):.2f} |"
        )
    return "\n".join(lines).rstrip() + "\n"


def _mermaid_label(value: Any) -> str:
    label = _clean_text(value, 80).replace('"', "'").replace("[", "(").replace("]", ")").replace("\n", " ")
    return re.sub(r"[^A-Za-z0-9_ .:/()'@-]", " ", label)


def export_mermaid_call_flow(
    graph: dict[str, Any] | None = None,
    *,
    project_id: str,
    snapshot_id: str | None = None,
    max_nodes: int = 100,
    max_edges: int = 200,
) -> str:
    payload = build_graph_export(
        graph,
        project_id=project_id,
        snapshot_id=snapshot_id,
        max_nodes=max_nodes,
        max_edges=max_edges,
    )
    call_edges = [
        edge
        for edge in payload["edges"]
        if any(marker in edge["relation_type"].upper() for marker in ("CALL", "INVOKE", "EXECUTE", "USE"))
    ]
    involved = sorted({value for edge in call_edges for value in (edge["source_id"], edge["target_id"])})
    node_by_id = {item["id"]: item for item in payload["nodes"]}
    aliases = {node_id: f"n{index}" for index, node_id in enumerate(involved)}
    lines = [
        "flowchart LR",
        f'  %% project_id={_mermaid_label(payload["project_id"])} snapshot_id={_mermaid_label(payload["snapshot_id"])}',
    ]
    for node_id in involved:
        node = node_by_id[node_id]
        lines.append(f'  {aliases[node_id]}["{_mermaid_label(node["title"] or node_id)}"]')
    for edge in call_edges:
        lines.append(
            f'  {aliases[edge["source_id"]]} -->|"{_mermaid_label(edge["relation_type"])}"| {aliases[edge["target_id"]]}'
        )
    return "\n".join(lines) + "\n"


def _safe_json_script(payload: dict[str, Any]) -> str:
    encoded = _json(payload)
    return encoded.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def export_graph_html(
    graph: dict[str, Any] | None = None,
    *,
    project_id: str,
    snapshot_id: str | None = None,
    max_nodes: int = 300,
    max_edges: int = 700,
) -> str:
    payload = build_graph_export(
        graph,
        project_id=project_id,
        snapshot_id=snapshot_id,
        max_nodes=max_nodes,
        max_edges=max_edges,
    )
    data = _safe_json_script(payload)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<title>MCUM Graph Explorer</title>
<style>
*{{box-sizing:border-box}} body{{margin:0;font:14px Arial,sans-serif;color:#17212b;background:#f4f6f8}}
header{{height:52px;display:flex;gap:12px;align-items:center;padding:8px 14px;background:#17212b;color:#fff}}
input,select{{height:34px;border:1px solid #aab4bf;background:#fff;padding:0 8px}} input{{min-width:240px}}
main{{display:grid;grid-template-columns:minmax(0,1fr) 320px;height:calc(100vh - 52px)}} canvas{{width:100%;height:100%;background:#fff}}
aside{{overflow:auto;border-left:1px solid #c8d0d8;padding:12px;background:#f8fafb}} h1{{font-size:16px;margin:0 auto 0 0}}
.row{{padding:8px 0;border-bottom:1px solid #dfe5ea;word-break:break-word}} .muted{{color:#586875}}
</style>
</head>
<body>
<header><h1>MCUM Graph Explorer</h1><input id="search" placeholder="Search entities"><select id="relations"><option value="">All relations</option></select></header>
<main><canvas id="graph"></canvas><aside><div id="summary"></div><div id="detail" class="row muted">Select a node.</div></aside></main>
<script>
'use strict';
const DATA={data};
const canvas=document.getElementById('graph'),ctx=canvas.getContext('2d'),search=document.getElementById('search'),relations=document.getElementById('relations');
const detail=document.getElementById('detail'),summary=document.getElementById('summary');
const types=[...new Set(DATA.edges.map(e=>e.relation_type))].sort(); types.forEach(t=>{{const o=document.createElement('option');o.value=t;o.textContent=t;relations.appendChild(o)}});
summary.textContent=`Project ${{DATA.project_id}} | ${{DATA.nodes.length}} nodes | ${{DATA.edges.length}} edges${{DATA.budget.truncated?' | truncated':''}}`;
let points=[]; function resize(){{const r=canvas.getBoundingClientRect();canvas.width=Math.max(1,r.width*devicePixelRatio);canvas.height=Math.max(1,r.height*devicePixelRatio);draw()}}
function visible(){{const q=search.value.toLowerCase(),rel=relations.value;const edges=DATA.edges.filter(e=>!rel||e.relation_type===rel);const linked=new Set(edges.flatMap(e=>[e.source_id,e.target_id]));const nodes=DATA.nodes.filter(n=>(!q||(n.title+' '+n.relative_path).toLowerCase().includes(q))&&(!rel||linked.has(n.id)));return {{nodes,edges}}}}
function draw(){{const v=visible(),w=canvas.width,h=canvas.height,cx=w/2,cy=h/2,r=Math.max(20,Math.min(w,h)*.38);ctx.clearRect(0,0,w,h);points=v.nodes.map((n,i)=>({{n,x:cx+Math.cos(2*Math.PI*i/Math.max(1,v.nodes.length))*r,y:cy+Math.sin(2*Math.PI*i/Math.max(1,v.nodes.length))*r}}));const byId=new Map(points.map(p=>[p.n.id,p]));ctx.strokeStyle='#aab4bf';v.edges.forEach(e=>{{const a=byId.get(e.source_id),b=byId.get(e.target_id);if(a&&b){{ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke()}}}});points.forEach(p=>{{ctx.fillStyle='#087e8b';ctx.beginPath();ctx.arc(p.x,p.y,6*devicePixelRatio,0,Math.PI*2);ctx.fill()}})}}
canvas.addEventListener('click',ev=>{{const r=canvas.getBoundingClientRect(),x=(ev.clientX-r.left)*devicePixelRatio,y=(ev.clientY-r.top)*devicePixelRatio;const p=points.find(p=>Math.hypot(p.x-x,p.y-y)<12*devicePixelRatio);if(p){{detail.textContent=`${{p.n.title}} | ${{p.n.entity_type}} | ${{p.n.relative_path||'no path'}}`}}}});
search.addEventListener('input',draw);relations.addEventListener('change',draw);addEventListener('resize',resize);resize();
</script>
</body>
</html>
"""


def export_graph(
    graph: dict[str, Any] | None = None,
    *,
    project_id: str,
    export_format: str,
    **kwargs: Any,
) -> str:
    formats = {
        "json": export_graph_json,
        "ndjson": export_graph_ndjson,
        "markdown": export_wiki_markdown,
        "wiki": export_wiki_markdown,
        "mermaid": export_mermaid_call_flow,
        "html": export_graph_html,
    }
    selected = formats.get(_text(export_format).lower())
    if selected is None:
        raise ValueError(f"unsupported export_format: {export_format}")
    return selected(graph, project_id=project_id, **kwargs)


export_json = export_graph_json
export_ndjson = export_graph_ndjson
export_wiki = export_wiki_markdown
export_call_flow = export_mermaid_call_flow
export_html = export_graph_html
