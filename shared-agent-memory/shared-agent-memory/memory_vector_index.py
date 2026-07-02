#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, json, os, re, time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
import lancedb, requests
ROOTS=[Path('/root/obsidian-vault/agents'), Path('/root/obsidian-vault/shared-memory'), Path('/root/obsidian-vault/plans'), Path('/root/obsidian-vault/projects'), Path('/root/obsidian-vault/wiki')]
DB_DIR=Path('/opt/shared-agent-memory/vector-index')
TABLE='memory_chunks'
OLLAMA_URL=os.environ.get('OLLAMA_EMBED_URL','http://[REDACTED_IP]:11434/api/embeddings')
EMBED_MODEL=os.environ.get('MEMORY_EMBED_MODEL','nomic-embed-text')
MAX_CHARS=2600; OVERLAP=120; DEFAULT_LIMIT=8
SKIP_DIRS={'.git','.obsidian','.trash','node_modules','__pycache__'}; EXTENSIONS={'.md','.txt'}
@dataclass
class Chunk:
    id:str; path:str; title:str; text:str; mtime:float; source:str
def iter_files()->Iterable[Path]:
    for root in ROOTS:
        if not root.exists(): continue
        for path in root.rglob('*'):
            if path.is_file() and not any(part in SKIP_DIRS for part in path.parts) and path.suffix.lower() in EXTENSIONS:
                yield path
def clean_text(text:str)->str:
    text=re.sub(r'```.*?```',' ',text,flags=re.S); text=re.sub(r'<!--.*?-->',' ',text,flags=re.S); text=re.sub(r'\n{3,}','\n\n',text)
    return text.strip()
def split_text(text:str)->list[str]:
    text=clean_text(text)
    if len(text)<=MAX_CHARS: return [text] if text else []
    chunks=[]; start=0
    while start<len(text):
        end=min(len(text),start+MAX_CHARS); window=text[start:end]
        cut=max(window.rfind('\n## '),window.rfind('\n\n'),window.rfind('。'),window.rfind('\n'))
        if cut>400 and end<len(text): end=start+cut+1
        chunk=text[start:end].strip()
        if chunk: chunks.append(chunk)
        if end>=len(text): break
        start=max(0,end-OVERLAP)
    return chunks
def file_title(path:Path,text:str)->str:
    for line in text.splitlines()[:12]:
        if line.startswith('# '): return line[2:].strip()
    return path.stem
def chunk_id(path:str,idx:int,text:str)->str:
    return hashlib.sha256(f'{path}:{idx}:{text}'.encode()).hexdigest()[:24]
def collect_chunks()->list[Chunk]:
    chunks=[]
    for path in iter_files():
        try: raw=path.read_text(encoding='utf-8',errors='ignore')
        except Exception: continue
        rel=str(path); title=file_title(path,raw); mtime=path.stat().st_mtime; source='shared-memory' if '/shared-memory/' in rel else 'obsidian'
        for idx,text in enumerate(split_text(raw)): chunks.append(Chunk(chunk_id(rel,idx,text),rel,title,text,mtime,source))
    return chunks
def embed(text:str)->list[float]:
    r=requests.post(OLLAMA_URL,json={'model':EMBED_MODEL,'prompt':text},timeout=120); r.raise_for_status(); vector=r.json().get('embedding')
    if not vector: raise RuntimeError('No embedding returned')
    return vector
def connect_table(recreate:bool=False):
    DB_DIR.mkdir(parents=True,exist_ok=True); db=lancedb.connect(str(DB_DIR)); names=db.table_names()
    if recreate and TABLE in names: db.drop_table(TABLE); names=db.table_names()
    if TABLE in names: return db.open_table(TABLE)
    return db.create_table(TABLE,data=[{'id':'__bootstrap__','path':'','title':'bootstrap','text':'bootstrap','source':'system','mtime':0.0,'updated_at':time.time(),'vector':embed('memory index bootstrap')}])
def build()->None:
    chunks=collect_chunks(); table=connect_table(recreate=True); rows=[]
    for i,chunk in enumerate(chunks,1):
        rows.append({'id':chunk.id,'path':chunk.path,'title':chunk.title,'text':chunk.text,'source':chunk.source,'mtime':chunk.mtime,'updated_at':time.time(),'vector':embed(chunk.text[:3000])})
        if len(rows)>=32: table.add(rows); rows.clear(); print(f'indexed {i}/{len(chunks)}')
    if rows: table.add(rows)
    try: table.delete("id = '__bootstrap__'")
    except Exception: pass
    meta={'chunks':len(chunks),'model':EMBED_MODEL,'updated_at':time.time(),'roots':[str(r) for r in ROOTS]}
    (DB_DIR/'metadata.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8'); print(json.dumps(meta,ensure_ascii=False,indent=2))
def search(query:str,limit:int)->None:
    table=connect_table(False); results=table.search(embed(query)).limit(limit).to_list()
    for idx,row in enumerate(results,1):
        text=row.get('text','').replace('\n',' ')
        if len(text)>360: text=text[:360]+'…'
        print(f'## {idx}. {row.get("title")}'); print(f'- path: {row.get("path")}'); print(f'- source: {row.get("source")}')
        if row.get('_distance') is not None: print(f'- distance: {row.get("_distance"):.4f}')
        print(f'- text: {text}\n')
def main():
    parser=argparse.ArgumentParser(description='Unified OpenClaw/Hermes memory vector index'); sub=parser.add_subparsers(dest='cmd',required=True); sub.add_parser('build'); s=sub.add_parser('search'); s.add_argument('query'); s.add_argument('--limit',type=int,default=DEFAULT_LIMIT); args=parser.parse_args()
    build() if args.cmd=='build' else search(args.query,args.limit)
if __name__=='__main__': main()
