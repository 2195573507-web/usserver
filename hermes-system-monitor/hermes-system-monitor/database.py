"""
SQLite database module for storing and querying system metrics.
Auto-creates tables, handles retention, and aggregation queries.
"""

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

DB_PATH = "/opt/hermes-system-monitor/metrics.db"

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    cpu_percent REAL,
    memory_percent REAL,
    memory_used_gb REAL,
    memory_total_gb REAL,
    disk_percent REAL,
    disk_used_gb REAL,
    disk_total_gb REAL,
    data_disk_percent REAL,
    data_disk_used_gb REAL,
    data_disk_total_gb REAL,
    network_rx_rate_mbps REAL,
    network_tx_rate_mbps REAL,
    load_1 REAL,
    load_5 REAL,
    load_15 REAL,
    disk_read_rate_mbps REAL,
    disk_write_rate_mbps REAL,
    swap_percent REAL,
    conntrack_percent REAL,
    fd_percent REAL,
    api_local_latency_ms REAL,
    api_apius_latency_ms REAL,
    api_subus_latency_ms REAL,
    api_warn_count REAL,
    api_bad_count REAL,
    docker_sub2api_cpu REAL,
    docker_sub2api_mem_mb REAL,
    docker_upstream_cpu REAL,
    docker_upstream_mem_mb REAL,
    docker_postgres_cpu REAL,
    docker_postgres_mem_mb REAL,
    docker_redis_cpu REAL,
    docker_redis_mem_mb REAL,
    docker_total_cpu REAL,
    docker_total_mem_mb REAL,
    http_2xx_count REAL,
    http_4xx_count REAL,
    http_5xx_count REAL,
    http_other_count REAL,
    db_total_mb REAL,
    wal_total_mb REAL,
    growth_root_gb_per_hour REAL,
    growth_www_gb_per_hour REAL,
    growth_docker_gb_per_hour REAL,
    growth_containerd_gb_per_hour REAL,
    psi_cpu_some_avg10 REAL,
    psi_io_some_avg10 REAL,
    psi_memory_some_avg10 REAL,
    disk_read_iops REAL,
    disk_write_iops REAL,
    disk_await_ms REAL,
    tcp_retrans_rate REAL,
    tcp_retrans_ratio REAL,
    systemd_failed_count REAL,
    sub2api_rx_kbps REAL,
    sub2api_tx_kbps REAL,
    pg_active_connections REAL,
    pg_max_connections REAL
);
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_metrics_timestamp ON metrics(timestamp);
CREATE INDEX IF NOT EXISTS idx_metrics_id_timestamp ON metrics(id, timestamp);
"""

CREATE_ROLLUP_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS metrics_rollup_1m (
    bucket TEXT PRIMARY KEY,
    cpu_percent REAL,
    memory_percent REAL,
    disk_percent REAL,
    data_disk_percent REAL,
    swap_percent REAL,
    network_rx_rate_mbps REAL,
    network_tx_rate_mbps REAL,
    load_1 REAL,
    load_5 REAL,
    load_15 REAL,
    disk_read_rate_mbps REAL,
    disk_write_rate_mbps REAL,
    conntrack_percent REAL,
    fd_percent REAL,
    api_local_latency_ms REAL,
    api_apius_latency_ms REAL,
    api_subus_latency_ms REAL,
    api_warn_count REAL,
    api_bad_count REAL,
    docker_sub2api_cpu REAL,
    docker_sub2api_mem_mb REAL,
    docker_upstream_cpu REAL,
    docker_upstream_mem_mb REAL,
    docker_postgres_cpu REAL,
    docker_postgres_mem_mb REAL,
    docker_redis_cpu REAL,
    docker_redis_mem_mb REAL,
    docker_total_cpu REAL,
    docker_total_mem_mb REAL,
    http_2xx_count REAL,
    http_4xx_count REAL,
    http_5xx_count REAL,
    http_other_count REAL,
    db_total_mb REAL,
    wal_total_mb REAL,
    growth_root_gb_per_hour REAL,
    growth_www_gb_per_hour REAL,
    growth_docker_gb_per_hour REAL,
    growth_containerd_gb_per_hour REAL,
    psi_cpu_some_avg10 REAL,
    psi_io_some_avg10 REAL,
    psi_memory_some_avg10 REAL,
    disk_read_iops REAL,
    disk_write_iops REAL,
    disk_await_ms REAL,
    tcp_retrans_rate REAL,
    tcp_retrans_ratio REAL,
    systemd_failed_count REAL,
    sub2api_rx_kbps REAL,
    sub2api_tx_kbps REAL,
    pg_active_connections REAL,
    pg_max_connections REAL,
    samples INTEGER NOT NULL DEFAULT 1
);
"""

CREATE_ROLLUP_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_metrics_rollup_1m_bucket ON metrics_rollup_1m(bucket);
"""


class MetricsDatabase:
    """Thread-safe SQLite database for metrics storage and queries."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._local = threading.local()
        self._history_cache = {}
        self._history_cache_lock = threading.Lock()

    def _get_conn(self) -> sqlite3.Connection:
        """Get a thread-local database connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=2.0)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn.execute("PRAGMA temp_store=MEMORY")
            self._local.conn.execute("PRAGMA mmap_size=268435456")  # 256MB
            self._local.conn.execute("PRAGMA cache_size=-16384")  # 16MB (increased from 8MB)
            self._local.conn.execute("PRAGMA busy_timeout=2000")  # 2 second busy timeout
        return self._local.conn

    def initialize(self):
        """Create the database and tables if they don't exist."""
        conn = self._get_conn()
        conn.execute(CREATE_TABLE_SQL)
        for column in ("sub2api_rx_kbps", "sub2api_tx_kbps", "data_disk_percent", "data_disk_used_gb", "data_disk_total_gb", "disk_read_rate_mbps", "disk_write_rate_mbps", "swap_percent", "conntrack_percent", "fd_percent", "api_local_latency_ms", "api_apius_latency_ms", "api_subus_latency_ms", "api_warn_count", "api_bad_count", "docker_sub2api_cpu", "docker_sub2api_mem_mb", "docker_upstream_cpu", "docker_upstream_mem_mb", "docker_postgres_cpu", "docker_postgres_mem_mb", "docker_redis_cpu", "docker_redis_mem_mb", "docker_total_cpu", "docker_total_mem_mb", "http_2xx_count", "http_4xx_count", "http_5xx_count", "http_other_count", "db_total_mb", "wal_total_mb", "growth_root_gb_per_hour", "growth_www_gb_per_hour", "growth_docker_gb_per_hour", "growth_containerd_gb_per_hour", "psi_cpu_some_avg10", "psi_io_some_avg10", "psi_memory_some_avg10", "disk_read_iops", "disk_write_iops", "disk_await_ms", "tcp_retrans_rate", "tcp_retrans_ratio", "systemd_failed_count", "sub2api_rx_kbps", "sub2api_tx_kbps", "pg_active_connections", "pg_max_connections"):
            try:
                conn.execute(f"ALTER TABLE metrics ADD COLUMN {column} REAL")
            except sqlite3.OperationalError:
                pass
        conn.executescript(CREATE_INDEX_SQL)
        conn.execute(CREATE_ROLLUP_TABLE_SQL)
        for column in ("api_local_latency_ms", "api_apius_latency_ms", "api_subus_latency_ms", "api_warn_count", "api_bad_count", "docker_sub2api_cpu", "docker_sub2api_mem_mb", "docker_upstream_cpu", "docker_upstream_mem_mb", "docker_postgres_cpu", "docker_postgres_mem_mb", "docker_redis_cpu", "docker_redis_mem_mb", "docker_total_cpu", "docker_total_mem_mb", "http_2xx_count", "http_4xx_count", "http_5xx_count", "http_other_count", "db_total_mb", "wal_total_mb", "growth_root_gb_per_hour", "growth_www_gb_per_hour", "growth_docker_gb_per_hour", "growth_containerd_gb_per_hour", "psi_cpu_some_avg10", "psi_io_some_avg10", "psi_memory_some_avg10", "disk_read_iops", "disk_write_iops", "disk_await_ms", "tcp_retrans_rate", "tcp_retrans_ratio", "systemd_failed_count", "sub2api_rx_kbps", "sub2api_tx_kbps", "pg_active_connections", "pg_max_connections"):
            try:
                conn.execute(f"ALTER TABLE metrics_rollup_1m ADD COLUMN {column} REAL")
            except sqlite3.OperationalError:
                pass
        conn.execute(CREATE_ROLLUP_INDEX_SQL)
        self._backfill_rollup_if_empty(conn)
        conn.commit()

    def _backfill_rollup_if_empty(self, conn: sqlite3.Connection):
        """Populate rollup table from existing raw metrics once after upgrade."""
        count = conn.execute("SELECT COUNT(*) FROM metrics_rollup_1m").fetchone()[0]
        if count:
            return
        conn.execute(
            """
            INSERT INTO metrics_rollup_1m (
                bucket, cpu_percent, memory_percent, disk_percent, data_disk_percent,
                swap_percent, network_rx_rate_mbps, network_tx_rate_mbps,
                load_1, load_5, load_15, disk_read_rate_mbps, disk_write_rate_mbps,
                conntrack_percent, fd_percent, samples
            )
            SELECT
                strftime('%Y-%m-%dT%H:%M:00+00:00', timestamp) AS bucket,
                AVG(cpu_percent), AVG(memory_percent), AVG(disk_percent), AVG(data_disk_percent),
                AVG(swap_percent), AVG(network_rx_rate_mbps), AVG(network_tx_rate_mbps),
                AVG(load_1), AVG(load_5), AVG(load_15), AVG(disk_read_rate_mbps), AVG(disk_write_rate_mbps),
                AVG(conntrack_percent), AVG(fd_percent), COUNT(*)
            FROM metrics
            GROUP BY bucket
            """
        )

    def insert(self, data: dict, timestamp: Optional[str] = None):
        """Insert a metrics snapshot into the database."""
        if timestamp is None:
            timestamp = datetime.now(timezone.utc).isoformat()

        sql = """
        INSERT INTO metrics (
            timestamp, cpu_percent, memory_percent, memory_used_gb, memory_total_gb,
            disk_percent, disk_used_gb, disk_total_gb,
            data_disk_percent, data_disk_used_gb, data_disk_total_gb,
            network_rx_rate_mbps, network_tx_rate_mbps,
            load_1, load_5, load_15, disk_read_rate_mbps, disk_write_rate_mbps,
            swap_percent, conntrack_percent, fd_percent,
            api_local_latency_ms, api_apius_latency_ms, api_subus_latency_ms, api_warn_count, api_bad_count,
            docker_sub2api_cpu, docker_sub2api_mem_mb, docker_upstream_cpu, docker_upstream_mem_mb,
            docker_postgres_cpu, docker_postgres_mem_mb, docker_redis_cpu, docker_redis_mem_mb,
            docker_total_cpu, docker_total_mem_mb, http_2xx_count, http_4xx_count, http_5xx_count, http_other_count,
            db_total_mb, wal_total_mb, growth_root_gb_per_hour, growth_www_gb_per_hour,
            growth_docker_gb_per_hour, growth_containerd_gb_per_hour,
            psi_cpu_some_avg10, psi_io_some_avg10, psi_memory_some_avg10, disk_read_iops, disk_write_iops, disk_await_ms,
            tcp_retrans_rate, tcp_retrans_ratio, systemd_failed_count,
            sub2api_rx_kbps, sub2api_tx_kbps, pg_active_connections, pg_max_connections
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

        cpu = data.get("cpu", {})
        memory = data.get("memory", {})
        disk = data.get("disk", {})
        data_disk = data.get("data_disk", {})
        network = data.get("network", {})
        load = data.get("load", {})
        swap = data.get("swap", {})
        conntrack = data.get("conntrack", {})
        fd_usage = data.get("fd_usage", {})
        api_links = data.get("api_links", {})
        api_items = {item.get("name"): item for item in api_links.get("items", [])}
        docker_resources = data.get("docker_resources", {})
        docker_items = {item.get("name"): item for item in docker_resources.get("containers", [])}
        http_errors = data.get("http_errors", {})
        database_status = data.get("database_status", {})
        growth_items = {item.get("path"): item for item in data.get("path_growth", {}).get("paths", [])}
        psi = data.get("psi_pressure", {})
        sub2api_io = data.get("sub2api_io", {})
        pg_conn = data.get("postgres_connections", {})
        tcp_retrans = data.get("tcp_retrans", {})
        systemd_failed = data.get("systemd_failed", {})

        params = (
            timestamp,
            cpu.get("percent"),
            memory.get("percent"),
            memory.get("used_gb"),
            memory.get("total_gb"),
            disk.get("percent"),
            disk.get("used_gb"),
            disk.get("total_gb"),
            data_disk.get("percent"),
            data_disk.get("used_gb"),
            data_disk.get("total_gb"),
            network.get("rx_rate_mbps"),
            network.get("tx_rate_mbps"),
            load.get("load_1"),
            load.get("load_5"),
            load.get("load_15"),
            disk.get("read_rate_mbps"),
            disk.get("write_rate_mbps"),
            swap.get("percent"),
            conntrack.get("percent"),
            fd_usage.get("percent"),
            api_items.get("local18888", {}).get("latency_ms"),
            api_items.get("apius", {}).get("latency_ms"),
            api_items.get("subus", {}).get("latency_ms"),
            api_links.get("summary", {}).get("warn"),
            api_links.get("summary", {}).get("bad"),
            docker_items.get("sub2api", {}).get("cpu_percent"),
            docker_items.get("sub2api", {}).get("mem_mb"),
            docker_items.get("upstream-hub-standalone", {}).get("cpu_percent"),
            docker_items.get("upstream-hub-standalone", {}).get("mem_mb"),
            docker_items.get("sub2api-postgres", {}).get("cpu_percent"),
            docker_items.get("sub2api-postgres", {}).get("mem_mb"),
            docker_items.get("sub2api-redis", {}).get("cpu_percent"),
            docker_items.get("sub2api-redis", {}).get("mem_mb"),
            docker_resources.get("total_cpu_percent"),
            docker_resources.get("total_mem_mb"),
            http_errors.get("status_2xx"),
            http_errors.get("status_4xx"),
            http_errors.get("status_5xx"),
            http_errors.get("status_other"),
            database_status.get("total_db_mb"),
            database_status.get("total_wal_mb"),
            growth_items.get("/", {}).get("growth_gb_per_hour"),
            growth_items.get("/www", {}).get("growth_gb_per_hour"),
            growth_items.get("/www/docker", {}).get("growth_gb_per_hour"),
            growth_items.get("/www/data/containerd", {}).get("growth_gb_per_hour"),
            psi.get("cpu", {}).get("some_avg10"),
            psi.get("io", {}).get("some_avg10"),
            psi.get("memory", {}).get("some_avg10"),
            disk.get("read_iops"),
            disk.get("write_iops"),
            disk.get("await_ms"),
            tcp_retrans.get("retrans_rate"),
            tcp_retrans.get("retrans_ratio"),
            systemd_failed.get("count"),
            sub2api_io.get("rx_rate_kbps"),
            sub2api_io.get("tx_rate_kbps"),
            pg_conn.get("active"),
            pg_conn.get("max"),
        )

        conn = self._get_conn()
        conn.execute(sql, params)
        self._upsert_rollup(conn, timestamp, params)
        conn.commit()

    def _upsert_rollup(self, conn: sqlite3.Connection, timestamp: str, params: tuple):
        """Maintain a 1-minute rollup while inserting raw points."""
        try:
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            dt = datetime.now(timezone.utc)
        bucket = dt.replace(second=0, microsecond=0).isoformat()
        values = {
            "cpu_percent": params[1],
            "memory_percent": params[2],
            "disk_percent": params[5],
            "data_disk_percent": params[8],
            "swap_percent": params[18],
            "network_rx_rate_mbps": params[11],
            "network_tx_rate_mbps": params[12],
            "load_1": params[13],
            "load_5": params[14],
            "load_15": params[15],
            "disk_read_rate_mbps": params[16],
            "disk_write_rate_mbps": params[17],
            "conntrack_percent": params[19],
            "fd_percent": params[20],
            "api_local_latency_ms": params[21],
            "api_apius_latency_ms": params[22],
            "api_subus_latency_ms": params[23],
            "api_warn_count": params[24],
            "api_bad_count": params[25],
            "docker_sub2api_cpu": params[26],
            "docker_sub2api_mem_mb": params[27],
            "docker_upstream_cpu": params[28],
            "docker_upstream_mem_mb": params[29],
            "docker_postgres_cpu": params[30],
            "docker_postgres_mem_mb": params[31],
            "docker_redis_cpu": params[32],
            "docker_redis_mem_mb": params[33],
            "docker_total_cpu": params[34],
            "docker_total_mem_mb": params[35],
            "http_2xx_count": params[36],
            "http_4xx_count": params[37],
            "http_5xx_count": params[38],
            "http_other_count": params[39],
            "db_total_mb": params[40],
            "wal_total_mb": params[41],
            "growth_root_gb_per_hour": params[42],
            "growth_www_gb_per_hour": params[43],
            "growth_docker_gb_per_hour": params[44],
            "growth_containerd_gb_per_hour": params[45],
            "psi_cpu_some_avg10": params[46],
            "psi_io_some_avg10": params[47],
            "psi_memory_some_avg10": params[48],
            "disk_read_iops": params[49],
            "disk_write_iops": params[50],
            "disk_await_ms": params[51],
            "tcp_retrans_rate": params[52],
            "tcp_retrans_ratio": params[53],
            "systemd_failed_count": params[54],
            "sub2api_rx_kbps": params[55],
            "sub2api_tx_kbps": params[56],
            "pg_active_connections": params[57],
            "pg_max_connections": params[58],
        }
        cols = list(values)
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(
            f"{c}=(({c} * samples) + excluded.{c}) / (samples + 1)" for c in cols
        )
        conn.execute(
            f"""
            INSERT INTO metrics_rollup_1m (bucket, {', '.join(cols)}, samples)
            VALUES (?, {placeholders}, 1)
            ON CONFLICT(bucket) DO UPDATE SET {updates}, samples=samples + 1
            """,
            (bucket, *[values[c] for c in cols]),
        )

    def get_latest(self) -> Optional[dict]:
        """Return the most recent metric row."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM metrics ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def get_history(self, range_str: str) -> dict:
        """
        Return historical data with appropriate aggregation.

        Aggregation strategy:
        - 1h: raw data sampled per 15 seconds
        - 24h: AVG aggregated per 5 minutes
        - 7d: AVG aggregated per 30 minutes
        """
        now_ts = time.time()
        cache_ttl = 4.0 if range_str == "1h" else 20.0
        with self._history_cache_lock:
            cached = self._history_cache.get(range_str)
            if cached and now_ts - cached[0] < cache_ttl:
                return cached[1]

        now = datetime.now(timezone.utc)
        if range_str == "1h":
            since = now - timedelta(hours=1)
            result = self._query_raw_sampled(since, 15)
        elif range_str == "24h":
            since = now - timedelta(hours=24)
            result = self._query_rollup(since, 300)  # 5 minute buckets
        elif range_str == "7d":
            since = now - timedelta(days=7)
            result = self._query_rollup(since, 1800)  # 30 minute buckets
        else:
            since = now - timedelta(hours=1)
            result = self._query_raw_sampled(since, 15)
        with self._history_cache_lock:
            self._history_cache[range_str] = (now_ts, result)
        return result

    def _query_raw(self, since: datetime) -> dict:
        """Return raw (unaggregated) data points."""
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT id, timestamp, cpu_percent, memory_percent, memory_used_gb, memory_total_gb,
                   disk_percent, disk_used_gb, disk_total_gb,
                   data_disk_percent, data_disk_used_gb, data_disk_total_gb,
                   network_rx_rate_mbps, network_tx_rate_mbps,
                   load_1, load_5, load_15, disk_read_rate_mbps, disk_write_rate_mbps,
                   swap_percent, conntrack_percent, fd_percent,
                   api_local_latency_ms, api_apius_latency_ms, api_subus_latency_ms, api_warn_count, api_bad_count,
                   docker_sub2api_cpu, docker_sub2api_mem_mb, docker_upstream_cpu, docker_upstream_mem_mb,
                   docker_postgres_cpu, docker_postgres_mem_mb, docker_redis_cpu, docker_redis_mem_mb,
                   docker_total_cpu, docker_total_mem_mb, http_2xx_count, http_4xx_count, http_5xx_count, http_other_count,
                   db_total_mb, wal_total_mb, growth_root_gb_per_hour, growth_www_gb_per_hour,
                   growth_docker_gb_per_hour, growth_containerd_gb_per_hour,
                   psi_cpu_some_avg10, psi_io_some_avg10, psi_memory_some_avg10, disk_read_iops, disk_write_iops, disk_await_ms,
                   tcp_retrans_rate, tcp_retrans_ratio, systemd_failed_count,
                   sub2api_rx_kbps, sub2api_tx_kbps,
                   pg_active_connections, pg_max_connections
            FROM metrics INDEXED BY idx_metrics_timestamp
            WHERE timestamp >= ? ORDER BY timestamp ASC
            """,
            (since.isoformat(),),
        ).fetchall()

        return self._rows_to_history(rows)

    def _query_raw_sampled(self, since: datetime, sample_seconds: int = 10) -> dict:
        """Return recent raw data sampled by time bucket to cap payload and JSON cost."""
        conn = self._get_conn()
        rows = conn.execute(
            f"""
            SELECT id, timestamp, cpu_percent, memory_percent, memory_used_gb, memory_total_gb,
                   disk_percent, disk_used_gb, disk_total_gb,
                   data_disk_percent, data_disk_used_gb, data_disk_total_gb,
                   network_rx_rate_mbps, network_tx_rate_mbps,
                   load_1, load_5, load_15, disk_read_rate_mbps, disk_write_rate_mbps,
                   swap_percent, conntrack_percent, fd_percent,
                   api_local_latency_ms, api_apius_latency_ms, api_subus_latency_ms, api_warn_count, api_bad_count,
                   docker_sub2api_cpu, docker_sub2api_mem_mb, docker_upstream_cpu, docker_upstream_mem_mb,
                   docker_postgres_cpu, docker_postgres_mem_mb, docker_redis_cpu, docker_redis_mem_mb,
                   docker_total_cpu, docker_total_mem_mb, http_2xx_count, http_4xx_count, http_5xx_count, http_other_count,
                   db_total_mb, wal_total_mb, growth_root_gb_per_hour, growth_www_gb_per_hour,
                   growth_docker_gb_per_hour, growth_containerd_gb_per_hour,
                   psi_cpu_some_avg10, psi_io_some_avg10, psi_memory_some_avg10, disk_read_iops, disk_write_iops, disk_await_ms,
                   tcp_retrans_rate, tcp_retrans_ratio, systemd_failed_count,
                   sub2api_rx_kbps, sub2api_tx_kbps,
                   pg_active_connections, pg_max_connections
            FROM metrics INDEXED BY idx_metrics_timestamp
            WHERE id IN (
                SELECT MAX(id)
                FROM metrics INDEXED BY idx_metrics_timestamp
                WHERE timestamp >= ?
                GROUP BY CAST(strftime('%s', timestamp) / {int(sample_seconds)} AS INTEGER)
            )
            ORDER BY timestamp ASC
            """,
            (since.isoformat(),),
        ).fetchall()
        return self._rows_to_history(rows)

    def _query_rollup(self, since: datetime, bucket_seconds: int) -> dict:
        """Return history from the maintained 1-minute rollup table."""
        conn = self._get_conn()
        if bucket_seconds <= 60:
            rows = conn.execute(
                """
                SELECT bucket, cpu_percent, memory_percent, disk_percent, data_disk_percent,
                       swap_percent, network_rx_rate_mbps, network_tx_rate_mbps,
                       load_1, load_5, load_15, disk_read_rate_mbps, disk_write_rate_mbps,
                       conntrack_percent, fd_percent,
                       api_local_latency_ms, api_apius_latency_ms, api_subus_latency_ms, api_warn_count, api_bad_count,
                       docker_sub2api_cpu, docker_sub2api_mem_mb, docker_upstream_cpu, docker_upstream_mem_mb,
                       docker_postgres_cpu, docker_postgres_mem_mb, docker_redis_cpu, docker_redis_mem_mb,
                       docker_total_cpu, docker_total_mem_mb, http_2xx_count, http_4xx_count, http_5xx_count, http_other_count,
                       db_total_mb, wal_total_mb, growth_root_gb_per_hour, growth_www_gb_per_hour,
                       growth_docker_gb_per_hour, growth_containerd_gb_per_hour,
                       psi_cpu_some_avg10, psi_io_some_avg10, psi_memory_some_avg10, disk_read_iops, disk_write_iops, disk_await_ms,
                       tcp_retrans_rate, tcp_retrans_ratio, systemd_failed_count,
                   sub2api_rx_kbps, sub2api_tx_kbps,
                   pg_active_connections, pg_max_connections
                FROM metrics_rollup_1m
                WHERE bucket >= ?
                ORDER BY bucket ASC
                """,
                (since.isoformat(),),
            ).fetchall()
            return self._rollup_rows_to_history(rows)

        sql = f"""
        SELECT
            strftime('%Y-%m-%dT%H:%M:%S+00:00', datetime((strftime('%s', bucket) / {bucket_seconds}) * {bucket_seconds}, 'unixepoch')) AS bucket_group,
            AVG(cpu_percent), AVG(memory_percent), AVG(disk_percent), AVG(data_disk_percent),
            AVG(swap_percent), AVG(network_rx_rate_mbps), AVG(network_tx_rate_mbps),
            AVG(load_1), AVG(load_5), AVG(load_15), AVG(disk_read_rate_mbps), AVG(disk_write_rate_mbps),
            AVG(conntrack_percent), AVG(fd_percent),
            AVG(api_local_latency_ms), AVG(api_apius_latency_ms), AVG(api_subus_latency_ms), AVG(api_warn_count), AVG(api_bad_count),
            AVG(docker_sub2api_cpu), AVG(docker_sub2api_mem_mb), AVG(docker_upstream_cpu), AVG(docker_upstream_mem_mb),
            AVG(docker_postgres_cpu), AVG(docker_postgres_mem_mb), AVG(docker_redis_cpu), AVG(docker_redis_mem_mb),
            AVG(docker_total_cpu), AVG(docker_total_mem_mb), AVG(http_2xx_count), AVG(http_4xx_count), AVG(http_5xx_count), AVG(http_other_count),
            AVG(db_total_mb), AVG(wal_total_mb), AVG(growth_root_gb_per_hour), AVG(growth_www_gb_per_hour),
            AVG(growth_docker_gb_per_hour), AVG(growth_containerd_gb_per_hour),
            AVG(psi_cpu_some_avg10), AVG(psi_io_some_avg10), AVG(psi_memory_some_avg10), AVG(disk_read_iops), AVG(disk_write_iops), AVG(disk_await_ms),
            AVG(tcp_retrans_rate), AVG(tcp_retrans_ratio), AVG(systemd_failed_count),
            AVG(sub2api_rx_kbps), AVG(sub2api_tx_kbps),
            AVG(pg_active_connections), AVG(pg_max_connections)
        FROM metrics_rollup_1m
        WHERE bucket >= ?
        GROUP BY bucket_group
        ORDER BY bucket_group ASC
        """
        return self._rollup_rows_to_history(conn.execute(sql, (since.isoformat(),)).fetchall())

    def _query_aggregated(self, since: datetime, bucket_seconds: int) -> dict:
        """Return data aggregated into time buckets using AVG."""
        conn = self._get_conn()

        # Round timestamps to bucket boundaries
        sql = f"""
        SELECT
            strftime('%Y-%m-%dT%H:%M:%SZ', 
                datetime(
                    (strftime('%s', timestamp) / {bucket_seconds}) * {bucket_seconds},
                    'unixepoch'
                )
            ) AS bucket,
            AVG(cpu_percent) AS cpu_percent,
            AVG(memory_percent) AS memory_percent,
            AVG(memory_used_gb) AS memory_used_gb,
            AVG(memory_total_gb) AS memory_total_gb,
            AVG(disk_percent) AS disk_percent,
            AVG(disk_used_gb) AS disk_used_gb,
            AVG(disk_total_gb) AS disk_total_gb,
            AVG(data_disk_percent) AS data_disk_percent,
            AVG(data_disk_used_gb) AS data_disk_used_gb,
            AVG(data_disk_total_gb) AS data_disk_total_gb,
            AVG(network_rx_rate_mbps) AS network_rx_rate_mbps,
            AVG(network_tx_rate_mbps) AS network_tx_rate_mbps,
            AVG(load_1) AS load_1,
            AVG(load_5) AS load_5,
            AVG(load_15) AS load_15,
            AVG(disk_read_rate_mbps) AS disk_read_rate_mbps,
            AVG(disk_write_rate_mbps) AS disk_write_rate_mbps,
            AVG(swap_percent) AS swap_percent,
            AVG(conntrack_percent) AS conntrack_percent,
            AVG(fd_percent) AS fd_percent,
            COUNT(*) AS count
        FROM metrics
        WHERE timestamp >= ?
        GROUP BY bucket
        ORDER BY bucket ASC
        """

        rows = conn.execute(sql, (since.isoformat(),)).fetchall()

        return self._agg_rows_to_history(rows)

    def cleanup(self, retention_days: int = 7):
        """Delete records older than the retention period."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        conn = self._get_conn()
        conn.execute("DELETE FROM metrics WHERE timestamp < ?", (cutoff.isoformat(),))
        deleted = conn.rowcount
        conn.execute("DELETE FROM metrics_rollup_1m WHERE bucket < ?", (cutoff.isoformat(),))
        conn.commit()
        if deleted:
            with self._history_cache_lock:
                self._history_cache.clear()
        return deleted

    def maintenance(self):
        """Run lightweight SQLite maintenance outside request handling."""
        conn = self._get_conn()
        conn.execute("PRAGMA optimize")
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

    # ── Helper methods ──────────────────────────────────────────

    @staticmethod
    def _row_to_dict(row) -> dict:
        """Convert a single DB row to a dict."""
        columns = [
            "id", "timestamp", "cpu_percent", "memory_percent",
            "memory_used_gb", "memory_total_gb", "disk_percent",
            "disk_used_gb", "disk_total_gb",
            "data_disk_percent", "data_disk_used_gb", "data_disk_total_gb",
            "network_rx_rate_mbps", "network_tx_rate_mbps",
            "load_1", "load_5", "load_15",
            "disk_read_rate_mbps", "disk_write_rate_mbps",
            "swap_percent", "conntrack_percent", "fd_percent",
            "api_local_latency_ms", "api_apius_latency_ms", "api_subus_latency_ms", "api_warn_count", "api_bad_count",
            "docker_sub2api_cpu", "docker_sub2api_mem_mb", "docker_upstream_cpu", "docker_upstream_mem_mb",
            "docker_postgres_cpu", "docker_postgres_mem_mb", "docker_redis_cpu", "docker_redis_mem_mb",
            "docker_total_cpu", "docker_total_mem_mb", "http_2xx_count", "http_4xx_count", "http_5xx_count", "http_other_count",
            "db_total_mb", "wal_total_mb", "growth_root_gb_per_hour", "growth_www_gb_per_hour",
            "growth_docker_gb_per_hour", "growth_containerd_gb_per_hour",
            "psi_cpu_some_avg10", "psi_io_some_avg10", "psi_memory_some_avg10", "disk_read_iops", "disk_write_iops", "disk_await_ms",
            "tcp_retrans_rate", "tcp_retrans_ratio", "systemd_failed_count",
            "sub2api_rx_kbps", "sub2api_tx_kbps",
            "pg_active_connections", "pg_max_connections",
        ]
        d = dict(zip(columns, row))
        return {
            "cpu": {"percent": d["cpu_percent"]},
            "memory": {
                "percent": d["memory_percent"],
                "used_gb": d["memory_used_gb"],
                "total_gb": d["memory_total_gb"],
            },
            "swap": {"percent": d.get("swap_percent")},
            "disk": {
                "percent": d["disk_percent"],
                "used_gb": d["disk_used_gb"],
                "total_gb": d["disk_total_gb"],
                "read_rate_mbps": d.get("disk_read_rate_mbps"),
                "write_rate_mbps": d.get("disk_write_rate_mbps"),
                "read_iops": d.get("disk_read_iops"),
                "write_iops": d.get("disk_write_iops"),
                "await_ms": d.get("disk_await_ms"),
            },
            "data_disk": {
                "percent": d.get("data_disk_percent"),
                "used_gb": d.get("data_disk_used_gb"),
                "total_gb": d.get("data_disk_total_gb"),
            },
            "network": {
                "rx_rate_mbps": d["network_rx_rate_mbps"],
                "tx_rate_mbps": d["network_tx_rate_mbps"],
            },
            "conntrack": {"percent": d.get("conntrack_percent")},
            "fd_usage": {"percent": d.get("fd_percent")},
            "load": {
                "load_1": d["load_1"],
                "load_5": d["load_5"],
                "load_15": d["load_15"],
            },
            "psi_pressure": {"cpu": {"some_avg10": d.get("psi_cpu_some_avg10")}, "io": {"some_avg10": d.get("psi_io_some_avg10")}, "memory": {"some_avg10": d.get("psi_memory_some_avg10")}},
            "tcp_retrans": {"retrans_rate": d.get("tcp_retrans_rate"), "retrans_ratio": d.get("tcp_retrans_ratio")},
            "systemd_failed": {"count": d.get("systemd_failed_count")},
            "sub2api_io": {"rx_rate_kbps": d.get("sub2api_rx_kbps"), "tx_rate_kbps": d.get("sub2api_tx_kbps")},
            "postgres_connections": {"active": d.get("pg_active_connections"), "max": d.get("pg_max_connections")},
            "timestamp": d["timestamp"],
        }

    @staticmethod
    def _rows_to_history(rows) -> dict:
        """Convert a list of DB rows into the history response format."""
        result = {
            "timestamps": [],
            "cpu": [],
            "memory": [],
            "disk": [],
            "data_disk": [],
            "swap": [],
            "network_rx": [],
            "network_tx": [],
            "disk_read": [],
            "disk_write": [],
            "conntrack": [],
            "fd": [],
            "load_1": [],
            "load_5": [],
            "load_15": [],
            "api_local_latency": [],
            "api_apius_latency": [],
            "api_subus_latency": [],
            "api_warn_count": [],
            "api_bad_count": [],
            "docker_sub2api_cpu": [],
            "docker_sub2api_mem": [],
            "docker_upstream_cpu": [],
            "docker_upstream_mem": [],
            "docker_postgres_cpu": [],
            "docker_postgres_mem": [],
            "docker_redis_cpu": [],
            "docker_redis_mem": [],
            "docker_total_cpu": [],
            "docker_total_mem": [],
            "http_2xx": [],
            "http_4xx": [],
            "http_5xx": [],
            "http_other": [],
            "db_total_mb": [],
            "wal_total_mb": [],
            "growth_root": [],
            "growth_www": [],
            "growth_docker": [],
            "growth_containerd": [],
            "psi_cpu": [],
            "psi_io": [],
            "psi_memory": [],
            "disk_read_iops": [],
            "disk_write_iops": [],
            "disk_await": [],
            "tcp_retrans_rate": [],
            "tcp_retrans_ratio": [],
            "systemd_failed": [],
            "sub2api_rx_kbps": [],
            "sub2api_tx_kbps": [],
            "pg_active_connections": [],
            "pg_max_connections": [],
        }
        columns = [
            "id", "timestamp", "cpu_percent", "memory_percent",
            "memory_used_gb", "memory_total_gb", "disk_percent",
            "disk_used_gb", "disk_total_gb",
            "data_disk_percent", "data_disk_used_gb", "data_disk_total_gb",
            "network_rx_rate_mbps", "network_tx_rate_mbps",
            "load_1", "load_5", "load_15",
            "disk_read_rate_mbps", "disk_write_rate_mbps",
            "swap_percent", "conntrack_percent", "fd_percent",
            "api_local_latency_ms", "api_apius_latency_ms", "api_subus_latency_ms", "api_warn_count", "api_bad_count",
            "docker_sub2api_cpu", "docker_sub2api_mem_mb", "docker_upstream_cpu", "docker_upstream_mem_mb",
            "docker_postgres_cpu", "docker_postgres_mem_mb", "docker_redis_cpu", "docker_redis_mem_mb",
            "docker_total_cpu", "docker_total_mem_mb", "http_2xx_count", "http_4xx_count", "http_5xx_count", "http_other_count",
            "db_total_mb", "wal_total_mb", "growth_root_gb_per_hour", "growth_www_gb_per_hour",
            "growth_docker_gb_per_hour", "growth_containerd_gb_per_hour",
            "psi_cpu_some_avg10", "psi_io_some_avg10", "psi_memory_some_avg10", "disk_read_iops", "disk_write_iops", "disk_await_ms",
            "tcp_retrans_rate", "tcp_retrans_ratio", "systemd_failed_count",
            "sub2api_rx_kbps", "sub2api_tx_kbps",
            "pg_active_connections", "pg_max_connections",
        ]
        for row in rows:
            d = dict(zip(columns, row))
            result["timestamps"].append(d["timestamp"])
            result["cpu"].append(d["cpu_percent"])
            result["memory"].append(d["memory_percent"])
            result["disk"].append(d["disk_percent"])
            result["data_disk"].append(d.get("data_disk_percent"))
            result["swap"].append(d.get("swap_percent"))
            result["network_rx"].append(d["network_rx_rate_mbps"])
            result["network_tx"].append(d["network_tx_rate_mbps"])
            result["disk_read"].append(d.get("disk_read_rate_mbps"))
            result["disk_write"].append(d.get("disk_write_rate_mbps"))
            result["conntrack"].append(d.get("conntrack_percent"))
            result["fd"].append(d.get("fd_percent"))
            result["load_1"].append(d["load_1"])
            result["load_5"].append(d["load_5"])
            result["load_15"].append(d["load_15"])
            result["api_local_latency"].append(d.get("api_local_latency_ms"))
            result["api_apius_latency"].append(d.get("api_apius_latency_ms"))
            result["api_subus_latency"].append(d.get("api_subus_latency_ms"))
            result["api_warn_count"].append(d.get("api_warn_count"))
            result["api_bad_count"].append(d.get("api_bad_count"))
            result["docker_sub2api_cpu"].append(d.get("docker_sub2api_cpu"))
            result["docker_sub2api_mem"].append(d.get("docker_sub2api_mem_mb"))
            result["docker_upstream_cpu"].append(d.get("docker_upstream_cpu"))
            result["docker_upstream_mem"].append(d.get("docker_upstream_mem_mb"))
            result["docker_postgres_cpu"].append(d.get("docker_postgres_cpu"))
            result["docker_postgres_mem"].append(d.get("docker_postgres_mem_mb"))
            result["docker_redis_cpu"].append(d.get("docker_redis_cpu"))
            result["docker_redis_mem"].append(d.get("docker_redis_mem_mb"))
            result["docker_total_cpu"].append(d.get("docker_total_cpu"))
            result["docker_total_mem"].append(d.get("docker_total_mem_mb"))
            result["http_2xx"].append(d.get("http_2xx_count"))
            result["http_4xx"].append(d.get("http_4xx_count"))
            result["http_5xx"].append(d.get("http_5xx_count"))
            result["http_other"].append(d.get("http_other_count"))
            result["db_total_mb"].append(d.get("db_total_mb"))
            result["wal_total_mb"].append(d.get("wal_total_mb"))
            result["growth_root"].append(d.get("growth_root_gb_per_hour"))
            result["growth_www"].append(d.get("growth_www_gb_per_hour"))
            result["growth_docker"].append(d.get("growth_docker_gb_per_hour"))
            result["growth_containerd"].append(d.get("growth_containerd_gb_per_hour"))
            result["psi_cpu"].append(d.get("psi_cpu_some_avg10"))
            result["psi_io"].append(d.get("psi_io_some_avg10"))
            result["psi_memory"].append(d.get("psi_memory_some_avg10"))
            result["disk_read_iops"].append(d.get("disk_read_iops"))
            result["disk_write_iops"].append(d.get("disk_write_iops"))
            result["disk_await"].append(d.get("disk_await_ms"))
            result["tcp_retrans_rate"].append(d.get("tcp_retrans_rate"))
            result["tcp_retrans_ratio"].append(d.get("tcp_retrans_ratio"))
            result["systemd_failed"].append(d.get("systemd_failed_count"))
            result["sub2api_rx_kbps"].append(d.get("sub2api_rx_kbps"))
            result["sub2api_tx_kbps"].append(d.get("sub2api_tx_kbps"))
            result["pg_active_connections"].append(d.get("pg_active_connections"))
            result["pg_max_connections"].append(d.get("pg_max_connections"))
        return result

    @staticmethod
    def _agg_rows_to_history(rows) -> dict:
        """Convert aggregated rows into the history response format."""
        result = {
            "timestamps": [],
            "cpu": [],
            "memory": [],
            "disk": [],
            "data_disk": [],
            "swap": [],
            "network_rx": [],
            "network_tx": [],
            "disk_read": [],
            "disk_write": [],
            "conntrack": [],
            "fd": [],
            "load_1": [],
            "load_5": [],
            "load_15": [],
            "api_local_latency": [],
            "api_apius_latency": [],
            "api_subus_latency": [],
            "api_warn_count": [],
            "api_bad_count": [],
            "docker_sub2api_cpu": [],
            "docker_sub2api_mem": [],
            "docker_upstream_cpu": [],
            "docker_upstream_mem": [],
            "docker_postgres_cpu": [],
            "docker_postgres_mem": [],
            "docker_redis_cpu": [],
            "docker_redis_mem": [],
            "docker_total_cpu": [],
            "docker_total_mem": [],
            "http_2xx": [],
            "http_4xx": [],
            "http_5xx": [],
            "http_other": [],
            "db_total_mb": [],
            "wal_total_mb": [],
            "growth_root": [],
            "growth_www": [],
            "growth_docker": [],
            "growth_containerd": [],
            "psi_cpu": [],
            "psi_io": [],
            "psi_memory": [],
            "disk_read_iops": [],
            "disk_write_iops": [],
            "disk_await": [],
            "tcp_retrans_rate": [],
            "tcp_retrans_ratio": [],
            "systemd_failed": [],
            "sub2api_rx_kbps": [],
            "sub2api_tx_kbps": [],
            "pg_active_connections": [],
            "pg_max_connections": [],
        }
        for row in rows:
            result["timestamps"].append(row[0])       # bucket
            result["cpu"].append(row[1])               # cpu_percent
            result["memory"].append(row[2])            # memory_percent
            result["disk"].append(row[5])              # disk_percent  (was row[4] — wrong!)
            result["data_disk"].append(row[8])         # data_disk_percent  (was row[7] — wrong!)
            result["swap"].append(row[18])             # swap_percent
            result["network_rx"].append(row[11])       # network_rx_rate_mbps  (was row[10])
            result["network_tx"].append(row[12])       # network_tx_rate_mbps  (was row[11])
            result["load_1"].append(row[13])           # load_1  (was row[12])
            result["load_5"].append(row[14])           # load_5  (was row[13])
            result["load_15"].append(row[15])          # load_15  (was row[14])
            result["disk_read"].append(row[16])        # disk_read_rate_mbps  (was row[15])
            result["disk_write"].append(row[17])       # disk_write_rate_mbps  (was row[16])
            result["conntrack"].append(row[19])        # conntrack_percent
            result["fd"].append(row[20])               # fd_percent
            result["sub2api_rx_kbps"].append(row[21] if len(row) > 21 else None)
            result["sub2api_tx_kbps"].append(row[22] if len(row) > 22 else None)
            result["pg_active_connections"].append(row[23] if len(row) > 23 else None)
            result["pg_max_connections"].append(row[24] if len(row) > 24 else None)
        return result

    @staticmethod
    def _rollup_rows_to_history(rows) -> dict:
        """Convert rollup rows into the history response format."""
        result = {
            "timestamps": [],
            "cpu": [],
            "memory": [],
            "disk": [],
            "data_disk": [],
            "swap": [],
            "network_rx": [],
            "network_tx": [],
            "disk_read": [],
            "disk_write": [],
            "conntrack": [],
            "fd": [],
            "load_1": [],
            "load_5": [],
            "load_15": [],
            "api_local_latency": [],
            "api_apius_latency": [],
            "api_subus_latency": [],
            "api_warn_count": [],
            "api_bad_count": [],
            "docker_sub2api_cpu": [],
            "docker_sub2api_mem": [],
            "docker_upstream_cpu": [],
            "docker_upstream_mem": [],
            "docker_postgres_cpu": [],
            "docker_postgres_mem": [],
            "docker_redis_cpu": [],
            "docker_redis_mem": [],
            "docker_total_cpu": [],
            "docker_total_mem": [],
            "http_2xx": [],
            "http_4xx": [],
            "http_5xx": [],
            "http_other": [],
            "db_total_mb": [],
            "wal_total_mb": [],
            "growth_root": [],
            "growth_www": [],
            "growth_docker": [],
            "growth_containerd": [],
            "psi_cpu": [],
            "psi_io": [],
            "psi_memory": [],
            "disk_read_iops": [],
            "disk_write_iops": [],
            "disk_await": [],
            "tcp_retrans_rate": [],
            "tcp_retrans_ratio": [],
            "systemd_failed": [],
            "sub2api_rx_kbps": [],
            "sub2api_tx_kbps": [],
            "pg_active_connections": [],
            "pg_max_connections": [],
        }
        for row in rows:
            result["timestamps"].append(row[0])
            result["cpu"].append(row[1])
            result["memory"].append(row[2])
            result["disk"].append(row[3])
            result["data_disk"].append(row[4])
            result["swap"].append(row[5])
            result["network_rx"].append(row[6])
            result["network_tx"].append(row[7])
            result["load_1"].append(row[8])
            result["load_5"].append(row[9])
            result["load_15"].append(row[10])
            result["disk_read"].append(row[11])
            result["disk_write"].append(row[12])
            result["conntrack"].append(row[13])
            result["fd"].append(row[14])
            result["api_local_latency"].append(row[15] if len(row) > 15 else None)
            result["api_apius_latency"].append(row[16] if len(row) > 16 else None)
            result["api_subus_latency"].append(row[17] if len(row) > 17 else None)
            result["api_warn_count"].append(row[18] if len(row) > 18 else None)
            result["api_bad_count"].append(row[19] if len(row) > 19 else None)
            result["docker_sub2api_cpu"].append(row[20] if len(row) > 20 else None)
            result["docker_sub2api_mem"].append(row[21] if len(row) > 21 else None)
            result["docker_upstream_cpu"].append(row[22] if len(row) > 22 else None)
            result["docker_upstream_mem"].append(row[23] if len(row) > 23 else None)
            result["docker_postgres_cpu"].append(row[24] if len(row) > 24 else None)
            result["docker_postgres_mem"].append(row[25] if len(row) > 25 else None)
            result["docker_redis_cpu"].append(row[26] if len(row) > 26 else None)
            result["docker_redis_mem"].append(row[27] if len(row) > 27 else None)
            result["docker_total_cpu"].append(row[28] if len(row) > 28 else None)
            result["docker_total_mem"].append(row[29] if len(row) > 29 else None)
            result["http_2xx"].append(row[30] if len(row) > 30 else None)
            result["http_4xx"].append(row[31] if len(row) > 31 else None)
            result["http_5xx"].append(row[32] if len(row) > 32 else None)
            result["http_other"].append(row[33] if len(row) > 33 else None)
            result["db_total_mb"].append(row[34] if len(row) > 34 else None)
            result["wal_total_mb"].append(row[35] if len(row) > 35 else None)
            result["growth_root"].append(row[36] if len(row) > 36 else None)
            result["growth_www"].append(row[37] if len(row) > 37 else None)
            result["growth_docker"].append(row[38] if len(row) > 38 else None)
            result["growth_containerd"].append(row[39] if len(row) > 39 else None)
            result["psi_cpu"].append(row[40] if len(row) > 40 else None)
            result["psi_io"].append(row[41] if len(row) > 41 else None)
            result["psi_memory"].append(row[42] if len(row) > 42 else None)
            result["disk_read_iops"].append(row[43] if len(row) > 43 else None)
            result["disk_write_iops"].append(row[44] if len(row) > 44 else None)
            result["disk_await"].append(row[45] if len(row) > 45 else None)
            result["tcp_retrans_rate"].append(row[46] if len(row) > 46 else None)
            result["tcp_retrans_ratio"].append(row[47] if len(row) > 47 else None)
            result["systemd_failed"].append(row[48] if len(row) > 48 else None)
            result["sub2api_rx_kbps"].append(row[49] if len(row) > 49 else None)
            result["sub2api_tx_kbps"].append(row[50] if len(row) > 50 else None)
            result["pg_active_connections"].append(row[51] if len(row) > 51 else None)
            result["pg_max_connections"].append(row[52] if len(row) > 52 else None)
        return result


# Module-level singleton
db = MetricsDatabase()
