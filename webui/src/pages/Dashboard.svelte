<script lang="ts">
  import { t } from '../lib/i18n'
  import { apiGet, apiPost } from '../lib/api'
  import { FINISHED, QUEUED, jobStore, type Job } from '../lib/jobs.svelte'

  interface Space {
    free_bytes: number | null
    total_bytes: number | null
  }
  interface Overview {
    version: string | null
    trackers: string[]
    roots: ({ path: string; name: string; library: boolean } & Space)[]
    tmp: ({ name: string } & Space) | null
    tools: { required: Record<string, boolean>; optional: Record<string, boolean> }
    jobs: Record<string, number>
    max_jobs: number
    config_warnings: string[]
    config_path: string | null
  }

  /** What the connection check found for one tracker (`salmon checkconf -t`). */
  interface TrackerCheck {
    tracker: string
    ok: boolean
    cookie: 'ok' | 'failed' | 'not_checked'
    cookie_error: string | null
    key: 'ok' | 'failed' | 'not_set' | 'not_checked'
    key_error: string | null
    tls_error: string | null
    checked_at: string
  }

  let overview = $state<Overview | null>(null)
  let error = $state('')
  let starting = $state(false)
  let checkError = $state('')

  // Read once: nothing here asks a tracker anything.
  $effect(() => {
    apiGet<Overview>('/dashboard')
      .then((answer) => (overview = answer))
      .catch((e) => (error = String(e)))
  })

  const missing = $derived(overview ? Object.values(overview.tools.required).filter((found) => !found).length : 0)

  // A warning is text, never HTML: split it around its URLs and render the links as anchors.
  function parts(line: string): { text: string; href?: string }[] {
    return line
      .split(/(https?:\/\/[^\s)]+)/)
      .filter(Boolean)
      .map((text) => (/^https?:\/\//.test(text) ? { text, href: text } : { text }))
  }

  function usedPercent(space: Space): number | null {
    if (space.free_bytes === null || !space.total_bytes) return null
    const percent = Math.round(((space.total_bytes - space.free_bytes) / space.total_bytes) * 100)
    return Math.min(100, Math.max(0, percent))
  }

  function formatBytes(bytes: number): string {
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB']
    let value = bytes
    let unit = 0
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024
      unit += 1
    }
    return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`
  }

  // The connection checks are jobs, kept by the server and followed by their events: the result stays until the
  // next run, across reloads too. Only the button starts one.
  const checkJobs = $derived(
    jobStore.jobs.filter((j) => j.kind === 'connection_check').sort((a, b) => b.created_at.localeCompare(a.created_at)),
  )
  const checking = $derived(checkJobs.find((j) => !FINISHED.includes(j.status)) ?? null)
  const lastRun = $derived<Job | null>(checkJobs.find((j) => FINISHED.includes(j.status)) ?? null)
  // The latest finished run only: a newer run that failed or was cancelled does not show an older run's rows.
  const checks = $derived<TrackerCheck[]>(
    ((lastRun?.status === 'done' ? (lastRun.result as { trackers?: TrackerCheck[] } | null)?.trackers : null) ??
      []) as TrackerCheck[],
  )

  async function checkConnections() {
    starting = true
    checkError = ''
    try {
      await apiPost<Job>('/jobs', { kind: 'connection_check', params: {} })
    } catch (e) {
      checkError = String(e)
    } finally {
      starting = false
    }
  }

  function when(iso: string): string {
    return new Date(iso).toLocaleString()
  }

  function problems(check: TrackerCheck): string[] {
    if (check.tls_error) return [check.tls_error]
    return [
      check.cookie_error ? `Session Cookie： ${check.cookie_error}` : '',
      check.key_error ? `API 密钥： ${check.key_error}` : '',
    ].filter(Boolean)
  }

  // Live, from the job events.
  const running = $derived(jobStore.jobs.filter((j) => j.status === 'running').length)
  const waiting = $derived(jobStore.jobs.filter((j) => j.status === 'waiting').length)
  const queued = $derived(jobStore.jobs.filter((j) => QUEUED.includes(j.status)).length)
  const finished = $derived(jobStore.jobs.filter((j) => FINISHED.includes(j.status)).length)
</script>

<!-- Ported from the fork's Dashboard (chodeus, 9bfdddc3, 0b29d2d5), without the connection check: no tracker request
     on load. -->

{#snippet usage(space: Space)}
  {@const used = usedPercent(space)}
  {#if used === null}
    <span class="chip">无法获取容量</span>
  {:else}
    <div class="disk" title="{formatBytes(space.free_bytes ?? 0)} 可用，总容量 {formatBytes(space.total_bytes ?? 0)}">
      <div class="disk-bar">
        <div class="disk-fill {used >= 90 ? 'err' : used >= 75 ? 'warn' : ''}" style="width: {used}%"></div>
      </div>
      <span class="muted">{formatBytes(space.free_bytes ?? 0)} 可用</span>
    </div>
  {/if}
{/snippet}

<h1>概览</h1>
<p class="lead">查看当前配置、站点连接及任务状态。</p>

{#if error}
  <div class="card"><p class="muted">无法加载概述： {error}</p></div>
{:else if !overview}
  <p class="muted">加载中…</p>
{:else}
  {#if overview.config_warnings.length}
    <div class="card">
      <h2>配置警告</h2>
      {#each overview.config_warnings as line (line)}
        <p class="small warning">
          {#each parts(t(line)) as part, i (i)}{#if part.href}<a href={part.href} target="_blank" rel="noopener noreferrer"
                >{part.text}</a
              >{:else}{part.text}{/if}{/each}
        </p>
      {/each}
      {#if overview.config_path}
        <p class="muted small">配置文件： <span class="mono">{overview.config_path}</span></p>
      {/if}
    </div>
  {/if}

  <div class="tiles">
    <div class="tile {running ? 'run' : ''}">
      <span class="figure">{running}<span class="of">/{overview.max_jobs}</span></span>
      <span class="muted">个任务正在运行</span>
    </div>
    <a class="tile {waiting ? 'warn' : ''}" href="#/jobs">
      <span class="figure">{waiting}</span>
      <span class="muted">等待回答</span>
    </a>
    <div class="tile">
      <span class="figure">{queued}</span>
      <span class="muted">排队等待</span>
    </div>
    <div class="tile">
      <span class="figure">{finished}</span>
      <span class="muted">已完成</span>
    </div>
  </div>

  <div class="card">
    <h2>smoked-salmon {overview.version ?? ''}</h2>
    <p class="row wrap">
      站点：
      {#each overview.trackers as tracker (tracker)}
        <span class="chip ok">{tracker}</span>
      {:else}
        <span class="chip err">未配置</span>
      {/each}
    </p>
  </div>

  <div class="card">
    <div class="row">
      <h2 class="grow">站点连接</h2>
      <button class="btn small" onclick={checkConnections} disabled={!overview.trackers.length || starting || !!checking}>
        {checking ? "检查中…" : "检查连接"}
      </button>
    </div>
    <p class="muted small">
      点击后，对上述站点执行与 <span class="mono">salmon checkconf -t</span> 相同的检查：先验证 Session，再验证已设置的 API 密钥。仅在点击按钮后发送请求。
    </p>
    {#if checkError}<p class="err-text small">{checkError}</p>{/if}
    {#if lastRun && lastRun.status !== 'done'}
      <p class="err-text small">
        上次检查 {lastRun.status === 'cancelled' ? "已取消" : `未完成： ${lastRun.error ?? lastRun.status}`}
      </p>
    {/if}
    {#if checks.length}
      <table>
        <tbody>
          {#each checks as check (check.tracker)}
            <tr>
              <td class="mono">{check.tracker}</td>
              <td>
                {#if check.cookie === 'ok'}<span class="chip ok">Session 正常</span>
                {:else if check.cookie === 'failed'}<span class="chip err">Session 验证失败</span>
                {:else}<span class="chip">尚未验证 Session</span>{/if}
              </td>
              <td>
                {#if check.key === 'ok'}<span class="chip ok">API 密钥正常</span>
                {:else if check.key === 'failed'}<span class="chip err">API 密钥验证失败</span>
                {:else if check.key === 'not_set'}<span class="chip">未设置 API 密钥</span>
                {:else}<span class="chip">尚未验证 API 密钥</span>{/if}
              </td>
              <td class="muted small">
                {#if check.tls_error}<span class="chip err">TLS 证书</span>{/if}
                {when(check.checked_at)}
              </td>
            </tr>
            {#each problems(check) as line (line)}
              <tr><td></td><td colspan="3" class="muted small problem">{t(line)}</td></tr>
            {/each}
          {/each}
        </tbody>
      </table>
    {:else if !checking && !lastRun}
      <p class="muted small">尚未检查。</p>
    {/if}
  </div>

  <div class="card">
    <h2>文件夹</h2>
    <p class="muted small">网页可浏览、任务可操作的文件夹。受保护音乐库中的原始专辑不会被修改。</p>
    <table>
      <tbody>
        {#each overview.roots as root (root.path)}
          <tr>
            <td>{root.library ? 'library_dirs' : 'download_directory'}</td>
            <td class="mono">{root.path}</td>
            <td>{@render usage(root)}</td>
          </tr>
        {/each}
        {#if overview.tmp}
          <tr>
            <td>临时目录</td>
            <td class="mono">{overview.tmp.name}</td>
            <td>{@render usage(overview.tmp)}</td>
          </tr>
        {/if}
      </tbody>
    </table>
  </div>

  <div class="card">
    <h2>工具</h2>
    <p class="muted small">
      {missing ? `未找到 ${missing} 个必需工具，请检查安装及 PATH 设置。` : "全部必需工具均可调用。"}
      检查方式同 <span class="mono">salmon health</span>.
    </p>
    <table>
      <tbody>
        {#each Object.entries(overview.tools.required) as [name, found] (name)}
          <tr>
            <td class="mono">{name}</td>
            <td><span class="chip {found ? 'ok' : 'err'}">{found ? "已找到" : "缺失"}</span></td>
          </tr>
        {/each}
        {#each Object.entries(overview.tools.optional) as [name, found] (name)}
          <tr>
            <td class="mono">{name} <span class="muted">(可选)</span></td>
            <td><span class="chip {found ? 'ok' : ''}">{found ? "已找到" : "缺失"}</span></td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
{/if}

<style>
  .tiles {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(160px, 100%), 1fr));
    gap: 0.7rem;
    margin-bottom: 0.9rem;
  }
  .tile {
    background: var(--bg-raised);
    border: 1px solid var(--border);
    border-left: 3px solid var(--border);
    border-radius: 10px;
    padding: 0.7rem 0.8rem;
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
    color: var(--text);
  }
  a.tile:hover {
    text-decoration: none;
    background: var(--bg-hover);
  }
  .tile.run {
    border-left-color: var(--accent);
  }
  .tile.warn {
    border-left-color: var(--warn);
  }
  .figure {
    font-size: 1.5rem;
    font-weight: 700;
    line-height: 1.1;
  }
  .figure .of {
    font-size: 0.9rem;
    font-weight: 500;
    color: var(--text-dim);
  }
  .wrap {
    flex-wrap: wrap;
    gap: 0.4rem;
  }
  .small {
    font-size: 0.85rem;
  }
  .warning {
    overflow-wrap: anywhere;
  }
  td.mono,
  td.problem {
    overflow-wrap: anywhere;
  }
  .err-text {
    color: var(--err);
  }
  .disk {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    min-width: 160px;
  }
  .disk-bar {
    flex: 1;
    height: 6px;
    border-radius: 3px;
    background: var(--border);
    overflow: hidden;
  }
  .disk-fill {
    height: 100%;
    background: var(--accent);
  }
  .disk-fill.warn {
    background: var(--warn);
  }
  .disk-fill.err {
    background: var(--err);
  }
</style>
