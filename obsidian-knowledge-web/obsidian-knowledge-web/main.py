"""
Obsidian Knowledge Web — server-local markdown vault browser.
Port: 9200
Vault: /root/obsidian-vault
"""

import os
import re
import time
import yaml
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

VAULT = Path(os.environ.get("OBSIDIAN_VAULT_PATH", "/root/obsidian-vault")).resolve()
STATIC_DIR = Path("/opt/obsidian-knowledge-web/static")
STATIC_DIR.mkdir(parents=True, exist_ok=True)

INDEX_TTL_SECONDS = int(os.environ.get("OBSIDIAN_INDEX_TTL_SECONDS", "30"))
MAX_FILE_BYTES = int(os.environ.get("OBSIDIAN_INDEX_MAX_FILE_BYTES", "1048576"))
_index_cache = {"built_at": 0.0, "signature": None, "items": []}


app = FastAPI(title="Obsidian Knowledge Web", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _safe_target(rel_path: str) -> Optional[Path]:
    target = (VAULT / rel_path).resolve()
    try:
        target.relative_to(VAULT)
    except Exception:
        return None
    return target


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from markdown, return (frontmatter_dict, content_without_fm)"""
    fm_match = re.match(r'^---\n(.*?)\n---\n', text, re.DOTALL)
    if not fm_match:
        return {}, text
    
    try:
        fm = yaml.safe_load(fm_match.group(1))
        if not isinstance(fm, dict):
            fm = {}
    except Exception:
        fm = {}
    
    content = text[fm_match.end():]
    return fm, content


def _file_info(path: Path) -> dict:
    rel = path.relative_to(VAULT).as_posix()
    stat = path.stat()
    try:
        text = path.read_text(errors="replace")
    except Exception:
        text = ""
    
    # Parse frontmatter
    frontmatter, content = _parse_frontmatter(text)
    
    # Extract title from frontmatter or first heading
    title = frontmatter.get('title') or path.stem
    for line in content.splitlines()[:30]:
        if line.startswith("# "):
            title = line.lstrip("# ").strip() or title
            break
    
    # Extract tags from frontmatter
    tags = frontmatter.get('tags', [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(',')]
    elif not isinstance(tags, list):
        tags = []
    
    category = rel.split("/", 1)[0] if "/" in rel else "root"
    
    return {
        "path": rel,
        "title": title,
        "category": category,
        "bytes": stat.st_size,
        "mtime": stat.st_mtime,
        "preview": content[:260].replace("\n", " ").strip(),
        "frontmatter": frontmatter,
        "tags": tags,
    }



def _all_markdown_files() -> list[Path]:
    if not VAULT.exists():
        return []
    return [p for p in VAULT.rglob("*.md") if p.is_file() and ".obsidian" not in p.parts]


def _vault_signature(files: list[Path]) -> tuple[int, float, int]:
    total_bytes = 0
    max_mtime = 0.0
    for p in files:
        try:
            st = p.stat()
        except Exception:
            continue
        total_bytes += st.st_size
        if st.st_mtime > max_mtime:
            max_mtime = st.st_mtime
    return (len(files), max_mtime, total_bytes)


def _tokenize(query: str) -> list[str]:
    return [t for t in re.split(r"\s+", query.lower().strip()) if t]


def _snippet(text: str, terms: list[str], width: int = 220) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    if not flat:
        return ""
    lower = flat.lower()
    positions = [lower.find(t) for t in terms if t and lower.find(t) >= 0]
    pos = min(positions) if positions else 0
    start = max(0, pos - width // 3)
    end = min(len(flat), start + width)
    snippet = flat[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(flat):
        snippet += "…"
    return snippet


def _search_score(info: dict, content: str, terms: list[str]) -> float:
    if not terms:
        return 0.0
    path = info.get("path", "").lower()
    title = str(info.get("title", "")).lower()
    tags = " ".join(map(str, info.get("tags", []))).lower()
    preview = info.get("preview", "").lower()
    body = content.lower()
    score = 0.0
    for term in terms:
        if term in title:
            score += 24
        if term in path:
            score += 14
        if term in tags:
            score += 10
        if term in preview:
            score += 6
        count = body.count(term)
        if count:
            score += min(20, count * 2)
    # Prefer current, curated, and smaller files when score ties.
    score += min(5, max(0, time.time() - 0) * 0)  # deterministic placeholder for future ranking knobs
    if info.get("category") in {"agents", "plans", "shared-memory", "运维备份", "记忆治理", "dashboards"}:
        score += 2
    if info.get("bytes", 0) < 12000:
        score += 1
    return score


def _build_index(force: bool = False) -> list[dict]:
    files = _all_markdown_files()
    signature = _vault_signature(files)
    now = time.time()
    if (
        not force
        and _index_cache["items"]
        and _index_cache["signature"] == signature
        and now - _index_cache["built_at"] < INDEX_TTL_SECONDS
    ):
        return _index_cache["items"]

    items = []
    for path in files:
        info = _file_info(path)
        content = ""
        if info.get("bytes", 0) <= MAX_FILE_BYTES:
            try:
                content = path.read_text(errors="replace")
            except Exception:
                content = ""
        frontmatter, body = _parse_frontmatter(content)
        headings = [line.lstrip("#").strip() for line in body.splitlines() if line.startswith("#")][:20]
        wikilinks = re.findall(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]", body)[:50]
        items.append({
            **info,
            "content": body,
            "headings": headings,
            "wikilinks": wikilinks,
        })
    _index_cache.update({"built_at": now, "signature": signature, "items": items})
    return items

@app.get("/api/status")
async def status():
    files = _all_markdown_files()
    categories = sorted({p.relative_to(VAULT).as_posix().split("/", 1)[0] if "/" in p.relative_to(VAULT).as_posix() else "root" for p in files})
    return {
        "vault": str(VAULT),
        "exists": VAULT.exists(),
        "files_count": len(files),
        "categories": categories,
        "index": {
            "cached_items": len(_index_cache.get("items") or []),
            "age_seconds": round(time.time() - _index_cache.get("built_at", 0), 1) if _index_cache.get("built_at") else None,
            "ttl_seconds": INDEX_TTL_SECONDS,
        },
    }


@app.get("/api/files")
async def files(q: str = "", category: str = "", limit: int = Query(300, ge=1, le=2000)):
    if not VAULT.exists():
        return {"vault": str(VAULT), "files": [], "count": 0, "categories": []}
    q_lower = q.lower().strip()
    items = []
    categories = set()
    for path in _all_markdown_files():
        if not path.is_file():
            continue
        info = _file_info(path)
        categories.add(info["category"])
        if category and info["category"] != category:
            continue
        # Search in path, title, preview, and tags
        hay = f"{info['path']} {info['title']} {info['preview']} {' '.join(info['tags'])}".lower()
        if q_lower and q_lower not in hay:
            continue
        items.append(info)
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"vault": str(VAULT), "files": items[:limit], "count": len(items), "categories": sorted(categories)}


@app.get("/api/file")
async def file(path: str):
    target = _safe_target(path)
    if not target or not target.exists() or target.suffix.lower() != ".md":
        return JSONResponse(status_code=404, content={"error": "file not found"})
    return {"file": _file_info(target), "content": target.read_text(errors="replace")}



@app.get("/api/search")
async def search(
    q: str = "",
    category: str = "",
    limit: int = Query(20, ge=1, le=100),
    include_content: bool = False,
):
    """Search path/title/tags/frontmatter/body with simple deterministic scoring."""
    terms = _tokenize(q)
    items = _build_index()
    results = []
    for item in items:
        if category and item.get("category") != category:
            continue
        score = _search_score(item, item.get("content", ""), terms) if terms else 1.0
        if terms and score <= 0:
            continue
        result = {k: v for k, v in item.items() if k not in {"content"}}
        result["score"] = round(score, 3)
        result["snippet"] = _snippet(item.get("content", "") or item.get("preview", ""), terms)
        if include_content:
            result["content"] = item.get("content", "")[:8000]
        results.append(result)
    results.sort(key=lambda x: (x["score"], x.get("mtime", 0)), reverse=True)
    return {"vault": str(VAULT), "query": q, "count": len(results), "results": results[:limit]}


@app.get("/api/context")
async def context(q: str, limit: int = Query(6, ge=1, le=20), chars: int = Query(900, ge=100, le=4000)):
    """Return compact RAG context snippets for agents."""
    terms = _tokenize(q)
    ranked = (await search(q=q, limit=limit, include_content=True))["results"]
    blocks = []
    for r in ranked:
        content = r.get("content") or r.get("snippet") or r.get("preview") or ""
        blocks.append({
            "source": r.get("path"),
            "title": r.get("title"),
            "score": r.get("score"),
            "snippet": _snippet(content, terms, width=min(chars, 1200)) or content[:chars],
            "tags": r.get("tags", []),
        })
    markdown = "\n\n".join(
        f"### {b['title']}\nSource: {b['source']} · score={b['score']}\n\n{b['snippet']}"
        for b in blocks
    )
    return {"query": q, "count": len(blocks), "blocks": blocks, "markdown": markdown}


@app.post("/api/reindex")
async def reindex():
    items = _build_index(force=True)
    return {"ok": True, "vault": str(VAULT), "indexed": len(items), "built_at": _index_cache["built_at"]}


@app.get("/api/graph")
async def graph(limit: int = Query(800, ge=1, le=3000)):
    if not VAULT.exists():
        return {"nodes": [], "links": [], "categories": [], "vault": str(VAULT)}
    md_files = _all_markdown_files()[:limit]
    by_stem = {p.stem: p.relative_to(VAULT).as_posix() for p in md_files}
    nodes = []
    links = []
    indeg = {}
    for path in md_files:
        rel = path.relative_to(VAULT).as_posix()
        category = rel.split("/", 1)[0] if "/" in rel else "root"
        nodes.append({"id": rel, "name": path.stem, "category": category, "symbolSize": 10})
    node_ids = {n["id"] for n in nodes}
    for path in md_files:
        rel = path.relative_to(VAULT).as_posix()
        try:
            text = path.read_text(errors="replace")
        except Exception:
            continue
        refs = re.findall(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]", text)
        refs += re.findall(r"\[[^\]]+\]\(([^)]+\.md)\)", text)
        for ref in refs:
            ref = ref.strip().replace("%20", " ")
            target = by_stem.get(Path(ref).stem)
            if not target:
                candidate = (path.parent / ref).resolve()
                try:
                    target = candidate.relative_to(VAULT).as_posix()
                except Exception:
                    target = None
            if target and target in node_ids and target != rel:
                links.append({"source": rel, "target": target})
                indeg[target] = indeg.get(target, 0) + 1
    for node in nodes:
        node["symbolSize"] = min(36, 10 + indeg.get(node["id"], 0) * 2)
    categories = sorted({n["category"] for n in nodes})
    return {"nodes": nodes, "links": links, "categories": categories, "vault": str(VAULT)}


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="[REDACTED_IP]", port=9200, log_level="info")
