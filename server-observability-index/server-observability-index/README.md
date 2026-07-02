# Server Observability Index

统一索引库，不替代原始文件，不改变服务读写路径。

## 文件

- 数据库：`/opt/server-observability-index/observability.sqlite`
- 索引脚本：`/opt/server-observability-index/index_observability.py`
- systemd service：`server-observability-index.service`
- systemd timer：`server-observability-index.timer`

## 已索引内容

| 表 | 来源 | 说明 |
|---|---|---|
| `sub2api_log_index` | `/opt/sub2api/data/logs/sub2api.log` | 日志行索引，含 timestamp/level/route/status/latency/upstream 粗解析 |
| `hermes_request_index` | `/root/.hermes/sessions/request_dump_*.json` | Hermes 请求 dump 索引，保留原始 JSON 文件 |
| `openclaw_session_index` | `/root/.openclaw/agents/main/sessions/sessions.json` | OpenClaw session 元数据索引 |
| `server_file_inventory` | `/opt`, `/root/.hermes`, `/root/.openclaw`, `/root/obsidian-vault`, `/root/server-organization` | 文件盘点索引，跳过 node_modules/.venv 等大依赖目录 |
| `memory_maintenance_report_index` | `/root/obsidian-vault/记忆治理/*.md` | 记忆维护报告索引 |
| `index_runs` | 本索引器 | 每次索引运行记录 |

## 常用查询

```bash
sqlite3 /opt/server-observability-index/observability.sqlite \
  "select status_code,count(*) from sub2api_log_index where status_code is not null group by status_code order by count(*) desc;"

sqlite3 /opt/server-observability-index/observability.sqlite \
  "select path,size,datetime(mtime,'unixepoch') from server_file_inventory order by size desc limit 20;"

sqlite3 /opt/server-observability-index/observability.sqlite \
  "select agent,report_date,hour,health_status,needs_human,path from memory_maintenance_report_index order by mtime desc limit 20;"
```

## 刷新

手动刷新：

```bash
/opt/server-observability-index/index_observability.py --mode all
```

只刷新某类：

```bash
/opt/server-observability-index/index_observability.py --mode logs
/opt/server-observability-index/index_observability.py --mode requests
/opt/server-observability-index/index_observability.py --mode sessions
/opt/server-observability-index/index_observability.py --mode files
/opt/server-observability-index/index_observability.py --mode memory-reports
```

## 安全边界

- 只读扫描源文件。
- 只写入本索引库。
- 不删除日志、JSON、Markdown、配置文件。
- 不迁移服务运行时状态。
- 不索引凭证明文语义，只保存文件路径和粗元数据；原始文件仍受系统权限保护。
