const $ = (id) => document.getElementById(id);
const pct = (value) => value === undefined || value === null || value === 'n/a' ? '--' : `${value}%`;
function metricValue(metrics, names){ for(const name of names){ if(metrics?.[name] !== undefined) return metrics[name]; } return undefined; }
function nestedMetric(metrics, group, names){ const obj = metrics?.[group]; if(obj && typeof obj === 'object'){ for(const name of names){ if(obj[name] !== undefined) return obj[name]; } } return undefined; }
function dotClass(status){ return status === 'online' ? 'ok' : status === 'degraded' ? 'warn' : ''; }
function renderService(service){
  const status = service.status || (service.online ? 'online' : 'offline');
  return `<article class="service" data-search="${[service.name,service.unit,service.port,service.path,service.group,service.exposure,...(service.tags||[])].join(' ').toLowerCase()}"><strong><span class="dot ${dotClass(status)}"></span>${service.name}</strong><div class="meta">${service.unit || '-'} · :${service.port || '-'} · ${service.exposure || 'unknown'}<br>${service.status_label || status} · ${service.systemd || 'unknown'}</div></article>`;
}
function addRisk(items, level, text){ items.push(`<div class="risk ${level}">${text}</div>`); }
async function load(){
  $('clock').textContent = new Date().toISOString().slice(11,16) + ' UTC';
  setInterval(() => $('clock').textContent = new Date().toISOString().slice(11,16) + ' UTC', 30000);
  let state;
  try{
    const res = await fetch('/api/state', {cache:'no-store'});
    state = await res.json();
  }catch(err){
    $('services').innerHTML = `<div class="risk bad">无法加载 /api/state：${err}</div>`;
    return;
  }
  const metrics = state.system?.metrics || {};
  const cpu = metricValue(metrics, ['cpu_percent']) ?? nestedMetric(metrics, 'cpu', ['percent','usage_percent']);
  const mem = metricValue(metrics, ['memory_percent']) ?? nestedMetric(metrics, 'memory', ['percent','used_percent']);
  const disk = metricValue(metrics, ['disk_percent']) ?? nestedMetric(metrics, 'disk', ['percent','used_percent']);
  $('metrics').innerHTML = `<article><span>CPU</span><strong>${pct(cpu)}</strong></article><article><span>内存</span><strong>${pct(mem)}</strong></article><article><span>磁盘</span><strong>${pct(disk)}</strong></article><article><span>服务</span><strong>${state.summary?.online ?? 0}/${state.summary?.total ?? 0}</strong></article>`;
  const services = state.projects || [];
  $('serviceSummary').textContent = `${state.summary?.online ?? 0} 正常 · ${state.summary?.degraded ?? 0} 降级 · ${state.summary?.offline ?? 0} 离线`;
  $('services').innerHTML = services.map(renderService).join('');
  const risks = [];
  if((state.summary?.offline || 0) > 0) addRisk(risks, 'bad', `有 ${state.summary.offline} 个服务离线，请查看服务矩阵。`);
  if((state.summary?.degraded || 0) > 0) addRisk(risks, 'warn', `有 ${state.summary.degraded} 个服务 HTTP 探测降级。`);
  for(const service of services){
    if(service.port && ['9000','9100','9200','9300','9400'].includes(String(service.port))) addRisk(risks, 'warn', `:${service.port} ${service.name} 当前服务进程监听 [REDACTED_IP]，建议后续收敛到本地监听或防火墙限制。`);
  }
  const memory = services.find(s => s.unit === 'shared-agent-memory');
  if(memory) addRisk(risks, memory.exposure === 'local-only' ? '' : 'bad', `shared-agent-memory 标记为 ${memory.exposure || 'unknown'}；写接口不得公网暴露。`);
  addRisk(risks, '', 'OpenClaw Gateway 只应保持 [REDACTED_IP] 监听；禁止直接反代原始控制口。');
  $('risks').innerHTML = risks.join('') || '<div class="risk">暂无风险提醒。</div>';
  $('apiBody').textContent = JSON.stringify({generated_at: state.generated_at, memory: state.knowledge?.memory, alerts: state.alerts}, null, 2);
  $('tasksBody').textContent = JSON.stringify({hermes_cron_jobs: state.hermes?.status?.cron_jobs_count ?? state.hermes?.overview?.cron_jobs_count ?? '见 Agent 控制台', openclaw_tasks: '通过 OpenClaw cron 工具查看；本页面第一版只读'}, null, 2);
}
function setupSearch(){
  const input = $('search');
  input?.addEventListener('input', () => {
    const q = input.value.trim().toLowerCase();
    document.querySelectorAll('.entry,.service').forEach(el => el.classList.toggle('hidden', q && !el.textContent.toLowerCase().includes(q) && !(el.dataset.keywords||el.dataset.search||'').includes(q)));
  });
  window.addEventListener('keydown', (event) => {
    if((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k'){ event.preventDefault(); input?.focus(); }
    if(event.key === 'Escape' && document.activeElement === input){ input.value=''; input.dispatchEvent(new Event('input')); input.blur(); }
  });
}
setupSearch();
load();
