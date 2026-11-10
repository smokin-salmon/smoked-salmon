<script lang="ts">
  import { t } from '../lib/i18n'
  import { apiGet, apiPost } from '../lib/api'
  import FolderPicker from '../lib/FolderPicker.svelte'
  import JobActivity from '../lib/JobActivity.svelte'
  import JobStatus from '../lib/JobStatus.svelte'
  import QuestionPanel from '../lib/QuestionPanel.svelte'
  import { FINISHED, jobStore, type Job } from '../lib/jobs.svelte'

  interface Options {
    trackers: string[]
    conversion_kinds: string[]
    max_releases: number
  }

  interface Uploaded {
    tracker: string
    format: string
    url: string
  }

  interface CrossUploadResult {
    source: string
    target: string
    inputs: string[]
    uploads: Uploaded[]
    // On a done job only.
    seedbox_failed?: boolean
    seedbox_failures?: {seedbox: string, step: string, item: string}[]
  }

  let options = $state<Options>({ trackers: [], conversion_kinds: [], max_releases: 5 })

  let inputs = $state('')
  let source = $state('')
  let target = $state('')
  let path = $state('')
  // Whether the picker has checked the folder and a job may start on it.
  let usable = $state(false)
  let groupId = $state('')
  // None ticked at each visit, as salmon cross-upload makes none without --conversions.
  let conversions = $state<string[]>([])
  let dryRun = $state(false)
  let assumeDefaults = $state(false)
  let showHelp = $state(false)

  let error = $state('')
  let starting = $state(false)
  let jobId = $state<string | null>(null)
  let showLog = $state(false)

  // The job started here, else the newest cross-upload: back on the page, the running one is shown again.
  const job = $derived(
    (jobId ? jobStore.get(jobId) : undefined) ?? jobStore.jobs.find((j) => j.kind === 'cross_upload'),
  )
  const live = $derived(job !== undefined && !FINISHED.includes(job.status))
  // Also for a job that stopped part way: what it uploaded before is up.
  const result = $derived(job ? (job.result as CrossUploadResult | null) : null)
  const given = $derived(inputs.split(/[\s,]+/).filter(Boolean))

  $effect(() => {
    apiGet<Options>('/cross-upload/options')
      .then((answer) => {
        options = answer
        if (!source && answer.trackers.length) source = answer.trackers[0]
        if (!target && answer.trackers.length > 1) target = answer.trackers[1]
      })
      .catch((e) => {
        error = `无法加载站点列表： ${e}`
      })
  })

  function toggleConversion(kind: string) {
    conversions = options.conversion_kinds.filter((k) => (k === kind) !== conversions.includes(k))
  }

  /** A group ID, or the ID in a torrents.php?id= link; null for none, undefined when it is neither. */
  function parseGroupId(value: string): number | null | undefined {
    const trimmed = value.trim()
    if (!trimmed) return null
    const fromUrl = /torrents\.php\?(?:.*&)?id=(\d+)/.exec(trimmed)
    const digits = fromUrl ? fromUrl[1] : trimmed
    return /^\d+$/.test(digits) && Number(digits) > 0 ? Number(digits) : undefined
  }

  async function start() {
    error = ''
    const group = parseGroupId(groupId)
    if (group === undefined) {
      error = `该 ${target} 群专辑组 ID必须是数字或torrents.php?id=链接。`
      return
    }
    starting = true
    try {
      const started = await apiPost<Job>('/jobs', {
        kind: 'cross_upload',
        params: {
          inputs: given,
          source,
          target,
          path: path.trim() || null,
          group_id: group,
          conversions,
        },
        dry_run: dryRun,
        assume_defaults: assumeDefaults,
      })
      jobId = started.id
      showLog = false
    } catch (e) {
      error = String(e)
    } finally {
      starting = false
    }
  }

  async function cancel() {
    if (!job) return
    try {
      await apiPost(`/jobs/${encodeURIComponent(job.id)}/cancel`)
    } catch (e) {
      error = String(e)
    }
  }

  function toggleLog() {
    showLog = !showLog
    if (showLog && job) void jobStore.show(job.id)
  }
</script>

<!-- Ported from the cross-upload panel of the fork's Tools page (chodeus, 0b29d2d5, 9bfdddc3 and 3c69045a): the
     torrents to copy instead of a folder, the folder only when it is not where the torrent's name says, and the job
     asking what salmon cross-upload asks, in the order it asks it. -->
<h1>转种</h1>
<p class="lead">
  在网页中执行 <span class="mono">salmon cross-upload</span>：读取来源站点的种子信息，核对本地文件，确认计划后上传到目标站点。模拟运行会读取两个站点的信息，不会提交上传。
</p>

{#if !live}
  <div class="card">
    <label>
      种子
      <input
        type="text"
        class="mono"
        bind:value={inputs}
        placeholder="种子 ID 或种子链接，以空格分隔"
      />
      {#if given.length > options.max_releases}
        <small class="hint warn">至多 {options.max_releases} 个种子／任务。</small>
      {:else if showHelp}
        <small class="hint">
          每项填写 {source || 'source'} torrent ID 或 torrents.php?torrentid= 链接，最多 {options.max_releases}.
        </small>
      {/if}
    </label>

    <div class="grid">
      <label>
        来源站点
        <select bind:value={source}>
          {#each options.trackers as code (code)}<option value={code}>{code}</option>{/each}
        </select>
        {#if showHelp}<small class="hint">种子所在的站点(SOURCE_TRACKER)。</small>{/if}
      </label>
      <label>
        目标站点
        <select bind:value={target}>
          {#each options.trackers as code (code)}<option value={code}>{code}</option>{/each}
        </select>
        {#if showHelp}<small class="hint">将它们上传到的站点（TARGET_TRACKER)。</small>{/if}
      </label>
      <label>
        专辑组 ID
        <input type="text" bind:value={groupId} placeholder="自动查找或新建专辑组" />
        {#if showHelp}
          <small class="hint">上传至此 {target || 'target'} 组：ID或torrents.php?id=链接（-g)。</small>
        {/if}
      </label>
    </div>

    <div class="field folder">
      <span class="label">专辑文件夹（可选）</span>
      <FolderPicker bind:value={path} bind:usable empty="每个 torrent 名称的文件夹，位于 download_directory 中" />
      {#if showHelp}
        <small class="hint">
          仅当文件不在 torrent 文件夹名称（--path）下的 download_directory 中时。只有一个种子。
        </small>
      {/if}
    </div>

    <div class="field conversions">
      <span class="label">转码格式</span>
      <div class="opts">
        {#each options.conversion_kinds as kind (kind)}
          <label class="check">
            <input type="checkbox" checked={conversions.includes(kind)} onchange={() => toggleConversion(kind)} />
            {kind}
          </label>
        {/each}
      </div>
      {#if showHelp}
        <small class="hint">
          同时将本地文件转码并上传到同一专辑组，与 salmon up 的 --conversions 相同。会跳过源 FLAC 无法生成的格式，以及该版本中已存在的格式。
        </small>
      {/if}
    </div>

    <div class="row wrap chips">
      <button class="chip toggle" class:on={dryRun} aria-pressed={dryRun} onclick={() => (dryRun = !dryRun)}>
        模拟运行
      </button>
      <button
        class="chip toggle"
        class:on={assumeDefaults}
        aria-pressed={assumeDefaults}
        onclick={() => (assumeDefaults = !assumeDefaults)}
      >
        -yyy
      </button>
      <label class="check help">
        <input type="checkbox" bind:checked={showHelp} /> 解释选项
      </label>
    </div>
    {#if showHelp}
      <p class="hint">
        模拟运行不发送任何内容：而是打印每个上传的表格。 -yyy 使用默认值回答是或否问题，如终端中所示；仍然会问没有默认的问题。
      </p>
    {/if}

    <div class="row actions">
      <button
        class="btn"
        onclick={start}
        disabled={!given.length || given.length > options.max_releases || !source || !target || (!!path && !usable) || starting}
      >
        {dryRun ? "开始模拟运行" : "开始转种"}
      </button>
    </div>
    {#if error}<p class="error">{error}</p>{/if}
  </div>
{/if}

{#if job}
  <div class="card">
    <div class="row head">
      <h2 class="grow">{t(job.title)}</h2>
      {#if live}
        <button class="btn small secondary" onclick={cancel}>取消</button>
      {:else}
        <button class="btn small secondary" onclick={toggleLog}>{showLog ? "隐藏日志" : "日志"}</button>
      {/if}
    </div>
    <JobStatus {job} />

    {#if result}
      {#if result.uploads.length}
        {#if job.status !== 'done'}<p class="muted">停止前上传：</p>{/if}
        {#if result.seedbox_failed}
          <p><span class="chip warn">远程做种服务器步骤失败（副本、客户端添加或无法设置客户端）：torrent 可能无处做种。查看日志。</span></p>
          {#if result.seedbox_failures?.length}
            <ul class="uploads">
              {#each result.seedbox_failures as failure}
                <li class="muted mono">{failure.step} 上 {failure.seedbox}: {failure.item}</li>
              {/each}
            </ul>
          {/if}
        {/if}
        <ul class="uploads">
          {#each result.uploads as each (each.url)}
            <li>
              <span class="chip ok">{each.tracker}</span>
              <span class="mono">{each.format}</span>
              <a href={each.url} target="_blank" rel="noreferrer">{each.url}</a>
            </li>
          {/each}
        </ul>
      {:else if job.status === 'done' && job.dry_run}
        <p class="muted">模拟运行：没有发送任何内容。它发送的表格在日志中。</p>
      {:else if job.status === 'done'}
        <p class="muted">没有上传任何内容。</p>
      {/if}
    {/if}

    <QuestionPanel {job} />
    {#if showLog || live}
      <JobActivity {job} logTail={showLog ? 0 : 40} />
    {/if}
  </div>
{/if}

<style>
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(200px, 100%), 1fr));
    gap: 0.6rem 1rem;
    margin-top: 0.8rem;
  }
  label,
  .field {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    font-size: 0.85rem;
    color: var(--text-dim);
  }
  label.check {
    flex-direction: row;
    align-items: center;
    gap: 0.35rem;
    color: var(--text);
  }
  .folder {
    margin-top: 0.8rem;
  }
  .wrap {
    flex-wrap: wrap;
    gap: 0.6rem;
  }
  .hint {
    display: block;
    color: var(--text-dim);
    font-size: 0.78rem;
    line-height: 1.35;
    margin: 0.15rem 0 0;
    max-width: 60ch;
  }
  .hint.warn {
    color: var(--err);
  }
  .conversions {
    margin-top: 0.9rem;
  }
  .opts {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem 1.1rem;
  }
  .chips {
    margin-top: 0.9rem;
    align-items: center;
  }
  .toggle {
    cursor: pointer;
    border: 1px solid var(--border);
    background: transparent;
    color: var(--text-dim);
    font: inherit;
  }
  .toggle.on {
    border-color: var(--accent);
    color: var(--text);
    background: var(--bg-hover);
  }
  .help {
    margin-left: auto;
  }
  .actions {
    margin-top: 0.9rem;
  }
  .error {
    color: var(--err);
    margin: 0.5rem 0 0;
  }
  .head {
    flex-wrap: wrap;
    margin-bottom: 0.4rem;
  }
  .head h2 {
    margin: 0;
    overflow-wrap: anywhere;
  }
  .uploads {
    list-style: none;
    padding: 0;
    margin: 0.8rem 0 0;
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
  }
  .uploads li {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    flex-wrap: wrap;
    overflow-wrap: anywhere;
  }
</style>
