#!/usr/bin/env python3
from __future__ import annotations
import argparse, subprocess
from pathlib import Path
INDEX=Path('/opt/shared-agent-memory/memory_vector_index.py'); PY=Path('/opt/shared-agent-memory/.venv/bin/python'); MODEL='qwen2.5:3b'
def recall(query:str,limit:int)->str:
    return subprocess.check_output([str(PY),str(INDEX),'search',query,'--limit',str(limit)],text=True)
def summarize(query:str,context:str)->str:
    prompt=f'''你是 OpenClaw 与 Hermes 的本地记忆压缩助手。请只根据下面检索到的记忆片段，压缩成给大模型使用的短上下文。\n\n要求：中文输出；最多8条；每条尽量不超过40字；保留路径、服务名、端口、配置、重要决策；删除重复、废话和不相关内容；不要编造。\n\n用户问题：{query}\n\n检索片段：\n{context}\n'''
    try: return subprocess.check_output(['ollama','run',MODEL,prompt],text=True,timeout=180)
    except Exception as exc: return f'[本地小模型不可用，返回原始检索结果]\n{exc}\n\n{context}'
def main():
    parser=argparse.ArgumentParser(description='Recall and compress OpenClaw/Hermes long-term memory'); parser.add_argument('query'); parser.add_argument('--limit',type=int,default=8); parser.add_argument('--raw',action='store_true'); args=parser.parse_args(); context=recall(args.query,args.limit); print(context if args.raw else summarize(args.query,context))
if __name__=='__main__': main()
