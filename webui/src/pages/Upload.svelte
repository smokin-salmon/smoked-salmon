<script lang="ts">
  import { t } from '../lib/i18n'
  import { apiGet, apiPost } from '../lib/api'
  import { sourcePreset } from '../lib/sourcePreset.svelte'
  import FolderPicker from '../lib/FolderPicker.svelte'
  import JobActivity from '../lib/JobActivity.svelte'
  import JobStatus from '../lib/JobStatus.svelte'
  import QuestionPanel from '../lib/QuestionPanel.svelte'
  import { FINISHED, jobStore, type Job } from '../lib/jobs.svelte'

  interface Options {
    trackers: string[]
    default_trackers: string[]
    sources: string[]
    encodings: string[]
    conversions: string[]
    conversion_kinds: string[]
    ai_review: boolean
  }

  interface Uploaded {
    tracker: string
    format: string
    url: string
  }

  interface UploadResult {
    folder: string
    trackers: string[]
    uploads: Uploaded[]
    // On a done job only.
    seedbox_failed?: boolean
    seedbox_failures?: {seedbox: string, step: string, item: string}[]
  }

  const OPTIONS_KEY = 'salmon.upload.options'

  // The only flags remembered between uploads: choices that do not depend on the album. Every other flag either
  // belongs to one album or skips a check or changes tags, and carried over silently it could send the next album
  // up unchecked. A flag added to FLAGS later is therefore not remembered unless it is added here.
  const REMEMBERED_FLAGS = ['auto_rename', 'spectrals_after', 'compress', 'essential_only', 'scene']

  interface Stored {
    flags: Record<string, boolean>
    assumeDefaults: boolean
    trackers: string[] | null
  }

  /** What was kept at the last Start. Anything unreadable falls back to the command's default, piece by piece. */
  function storedOptions(): Stored {
    const found: Stored = { flags: {}, assumeDefaults: false, trackers: null }
    try {
      const data: unknown = JSON.parse(localStorage.getItem(OPTIONS_KEY) ?? 'null')
      if (typeof data !== 'object' || data === null || Array.isArray(data)) return found
      const { flags: kept, assume_defaults: yyy, trackers: picked } = data as Record<string, unknown>
      if (typeof kept === 'object' && kept !== null && !Array.isArray(kept)) {
        for (const key of REMEMBERED_FLAGS) {
          const value = (kept as Record<string, unknown>)[key]
          if (value === true) found.flags[key] = true
        }
      }
      if (yyy === true) found.assumeDefaults = true
      if (Array.isArray(picked) && picked.every((each) => typeof each === 'string')) found.trackers = picked
    } catch {
      // Unreadable or blocked storage: the defaults.
    }
    return found
  }

  function saveOptions() {
    try {
      const kept: Record<string, boolean> = {}
      for (const key of REMEMBERED_FLAGS) kept[key] = !!flags[key]
      localStorage.setItem(
        OPTIONS_KEY,
        JSON.stringify({ flags: kept, assume_defaults: assumeDefaults, trackers: chosen }),
      )
    } catch {
      // Not remembered: the page works as before.
    }
  }

  function resetOptions() {
    try {
      localStorage.removeItem(OPTIONS_KEY)
    } catch {
      // Nothing was kept, or it cannot be reached.
    }
    for (const key of REMEMBERED_FLAGS) flags[key] = false
    assumeDefaults = false
    chosen = firstVisitTrackers(options)
    conversions = presetConversions(options)
    askConversions = false
  }

  /** The conversions ticked at a visit: the config's upload.conversions, those that are kinds. */
  function presetConversions(answer: Options): string[] {
    return (answer.conversions ?? []).filter((k) => (answer.conversion_kinds ?? []).includes(k))
  }

  /** The trackers ticked when nothing is remembered: the config's default_trackers, else the first configured. */
  function firstVisitTrackers(answer: Options): string[] {
    const defaults = (answer.default_trackers ?? []).filter((t) => answer.trackers.includes(t))
    if (defaults.length) return defaults
    return answer.trackers.length ? [answer.trackers[0]] : []
  }

  let options = $state<Options>({ trackers: [], default_trackers: [], sources: [], encodings: [], conversions: [], conversion_kinds: [], ai_review: false })

  let path = $state('')
  // Whether the picker has checked the folder and a job may start on it.
  let usable = $state(false)
  let chosen = $state<string[]>([])
  let source = $state('')
  let lossy = $state<'ask' | 'yes' | 'no'>('ask')
  let groupId = $state('')
  let request = $state('')
  let sourceUrl = $state('')
  const preset = sourcePreset(
    () => path,
    (found) => (source = found),
  )
  // Only with a store link: search the stores anyway, as -su without -ss does. This upload only.
  let searchAnyway = $state(false)
  // Transcodes only is --skip-flac-upload, whose group is the one the FLAC is already in.
  let mode = $state<'flac' | 'transcodes'>('flac')
  let encoding = $state('')
  let spectrals = $state('')
  // This upload only: preset from upload.conversions at every visit, never remembered.
  let conversions = $state<string[]>([])
  // Ask during the run instead: no selection is sent, and the job asks as the terminal does.
  let askConversions = $state(false)
  let flags = $state<Record<string, boolean>>(storedOptions().flags)
  let dryRun = $state(false)
  let assumeDefaults = $state(storedOptions().assumeDefaults)
  let showHelp = $state(false)

  let error = $state('')
  let starting = $state(false)
  let jobId = $state<string | null>(null)
  let showLog = $state(false)

  // The options of salmon up a form gives, as the job takes them; their defaults are the command's.
  const FLAGS: { key: string; label: string; help: string }[] = [
    { key: 'auto_rename', label: "自动重命名", help: "自动重命名文件和文件夹，无需再次确认（-n）。" },
    {
      key: 'spectrals_after',
      label: "上传后的频谱",
      help: "种子发布后检查、上传并报告频谱（-a）。模拟运行时不执行。",
    },
    {
      key: 'compress',
      label: "重新压缩 FLAC",
      help: "先按配置的压缩级别重新压缩 FLAC（-c），音频内容不变。",
    },
    {
      key: 'scene',
      label: "Scene 发行",
      help: "保留文件夹名、文件名、标签和封面（--scene）。不能与“仅保留必要文件”同时使用。",
    },
    {
      key: 'essential_only',
      label: "仅保留必要文件",
      help: "仅上传音频、抓轨日志、CUE 和封面；排除 nfo、sfv、md5、txt 等附加文件（-eo）。",
    },
    {
      key: 'overwrite',
      label: "覆盖元数据",
      help: "使用抓取结果中的艺术家、年份、厂牌、发行编号和流派覆盖文件标签（-ow）。",
    },
    { key: 'skip_up', label: "跳过虚假位深检查", help: "跳过将 16-bit 填充为 24-bit 的虚假位深检查（--skip-up）。" },
    { key: 'skip_mqa', label: "跳过MQA检查", help: "跳过对MQA标记的检查(--skip-mqa)。" },
    { key: 'skip_log_check', label: "跳过日志检查", help: "跳过 CD 抓轨日志评分（--skip-log-check）。" },
    {
      key: 'skip_integrity_check',
      label: "跳过完整性检查",
      help: "跳过音频文件的解码完整性检查（--skip-integrity-check）。",
    },
    {
      key: 'skip_initial_review',
      label: "跳过首次人工核对",
      help: "在AI审查之前跳过手动元数据审查(--skip-initial-review)。",
    },
    {
      key: 'apply_ai_suggestions',
      label: "应用 AI 修改建议",
      help: "无需询问即可应用AI审阅的编辑（--apply-ai-suggestions）。",
    },
  ]

  const transcodes = $derived(mode === 'transcodes')
  // The command refuses these with --skip-flac-upload: hidden and not sent in that mode, their values kept.
  const NOT_WITH_TRANSCODES = ['spectrals_after']
  const AI_FLAGS = ['skip_initial_review', 'apply_ai_suggestions']
  /** The flags the form shows: the AI ones only with upload.ai_review on. */
  const shownFlags = $derived(FLAGS.filter(
      (f) => (options.ai_review || !AI_FLAGS.includes(f.key)) && !(transcodes && NOT_WITH_TRANSCODES.includes(f.key)),
    ))

  /** The overrides that differ from their default, by their label: what the Overrides summary names. */
  const overridden = $derived.by(() => {
    const set: string[] = []
    if (mode === 'flac' && groupId.trim()) set.push("专辑组 ID")
    if (!transcodes && request.trim()) set.push('求种')
    if (lossy !== 'ask') set.push("有损母带")
    if (encoding) set.push('编码')
    if (spectrals.trim()) set.push("频谱曲目")
    if (sourceUrl.trim() && searchAnyway) set.push("无论如何都要搜索商店")
    for (const flag of shownFlags) if (flags[flag.key]) set.push(flag.label.toLowerCase())
    return set
  })
  const summary = $derived(overridden.length ? `本次上传选项（${overridden.length} 项已设置： ${overridden.join(', ')})` : "本次上传选项")
  const canStart = $derived(
    usable &&
      !!source &&
      (transcodes ? chosen.length === 1 : chosen.length > 0) &&
      !starting &&
      (!transcodes || (parseGroupId(groupId) != null && (askConversions || conversions.length > 0))),
  )

  // The job started here, else the newest upload: back on the page, the running upload is shown again.
  // Ported from the fork's Upload page (chodeus, 889cc4f5).
  const job = $derived(
    (jobId ? jobStore.get(jobId) : undefined) ?? jobStore.jobs.find((j) => j.kind === 'upload'),
  )
  const live = $derived(job !== undefined && !FINISHED.includes(job.status))
  const result = $derived(job ? (job.result as UploadResult | null) : null)

  $effect(() => {
    apiGet<Options>('/upload/options')
      .then((answer) => {
        options = answer
        conversions = presetConversions(answer)
        if (chosen.length || !answer.trackers.length) return
        const kept = storedOptions().trackers
        // In the order kept, which is the order the job uploads in.
        const still = kept ? kept.filter((t) => answer.trackers.includes(t)) : []
        // A stored empty list stays empty; nothing stored, or all gone from the config: the first-visit preset.
        chosen = kept && (kept.length === 0 || still.length) ? still : firstVisitTrackers(answer)
      })
      .catch((e) => {
        error = `无法加载站点列表： ${e}`
      })
  })

  // Kept in the order the config lists them, which is the order the job uploads in.
  function toggleTracker(code: string) {
    chosen = chosen.includes(code)
      ? chosen.filter((c) => c !== code)
      : options.trackers.filter((t) => t === code || chosen.includes(t))
  }

  // Kept in the canonical order, which is the order the job makes them in.
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

  /** The flags as sent: the AI ones false when upload.ai_review is off, whatever was ticked before. */
  function sentFlags(): Record<string, boolean> {
    const sent: Record<string, boolean> = {}
    for (const flag of FLAGS) {
      const hidden = (!options.ai_review && AI_FLAGS.includes(flag.key)) || (transcodes && NOT_WITH_TRANSCODES.includes(flag.key))
      sent[flag.key] = !!flags[flag.key] && !hidden
    }
    return sent
  }

  async function start() {
    error = ''
    const group = parseGroupId(groupId)
    if (group === undefined || (transcodes && group === null)) {
      error = transcodes
        ? "转码仅需要FLAC所在的组：数字或torrents.php?id=链接。"
        : "群专辑组 ID必须是数字或torrents.php?id=链接。"
      return
    }
    const tracks = spectrals.split(/[,\s]+/).filter(Boolean).map(Number)
    if (tracks.some((n) => !Number.isInteger(n) || n < 1)) {
      error = "频谱曲目号是从1开始的整数。"
      return
    }
    saveOptions()
    starting = true
    const sent = path
    try {
      const started = await apiPost<Job>('/jobs', {
        kind: 'upload',
        params: {
          path: sent,
          source,
          trackers: chosen,
          group_id: group,
          request: transcodes ? null : request.trim() || null,
          source_url: sourceUrl.trim() || null,
          // A store link is taken as it is (-su -ss), unless the stores are to be searched too.
          skip_search: !!sourceUrl.trim() && !searchAnyway,
          skip_flac_upload: transcodes,
          encoding: encoding || null,
          lossy: lossy === 'ask' ? null : lossy === 'yes',
          spectrals: tracks,
          conversions: askConversions ? null : conversions,
          ...sentFlags(),
        },
        dry_run: dryRun,
        assume_defaults: assumeDefaults,
      })
      jobId = started.id
      showLog = false
      // A real upload has taken the folder and may move or remove it: the next one starts with nothing chosen,
      // unless another was typed meanwhile. A dry run leaves it, to be run for real.
      if (!dryRun && path === sent) path = ''
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

<!-- Ported from the fork's Upload page (styx-techno 637ee666; chodeus 0b29d2d5, 889cc4f5, 6f846b37), without its
     pre-flight step: the job asks what salmon up asks, in the order it asks it. -->
<h1>上传</h1>
<p class="lead">
  在网页中执行 <span class="mono">salmon up</span>：检查专辑、核对元数据，并按步骤确认上传选项。模拟运行会在专辑副本上完成检查并显示上传表单，不会提交上传。
</p>

{#if !live}
  <div class="card">
    <FolderPicker bind:value={path} bind:usable />

    <div class="grid">
      <div class="field">
        <span class="label">上传站点（按顺序）</span>
        <div class="row wrap">
          {#each options.trackers as code (code)}
            <label class="check">
              <input type="checkbox" checked={chosen.includes(code)} onchange={() => toggleTracker(code)} />
              {code}
            </label>
          {/each}
        </div>
        <small class="hint">
          选择至少一个。上传将按顺序准确地发送至已勾选的站点，并且不会询问其他信息 (-t)。
        </small>
      </div>
      <label>
        来源介质
        <select bind:value={source} onchange={() => preset.picked()}>
          <option value="" disabled>选择一个</option>
          {#each options.sources as each (each)}<option value={each}>{each}</option>{/each}
        </select>
        {#if preset.reason}<small class="hint muted">{t(preset.reason)}</small>{/if}
        {#if showHelp}<small class="hint">文件来自的介质(-s)。</small>{/if}
      </label>
      <label>
        发行页面链接
        <input type="text" bind:value={sourceUrl} placeholder="https://" />
        <small class="hint">
          优先读取此链接中的元数据；无法读取时再搜索其他商店。{#if showHelp}
            (-su -ss){/if}
        </small>
      </label>
      <div class="field">
        <span class="label">转码格式</span>
        <div class="row wrap">
          {#each options.conversion_kinds as kind (kind)}
            <label class="check">
              <input type="checkbox" disabled={askConversions} checked={conversions.includes(kind)} onchange={() => toggleConversion(kind)} />
              {kind}
            </label>
          {/each}
          <label class="check">
            <input type="checkbox" bind:checked={askConversions} />
            运行时询问
          </label>
        </div>
        <small class="hint">
          仅对本次上传生效。上传 FLAC 后生成勾选的格式；选择“运行时询问”则在搜索专辑组后询问，并使用 upload.conversions 作为默认选项。{#if showHelp}
            （--conversions，未勾选则运行时询问）{/if}
        </small>
      </div>
    </div>

    <div class="field mode">
      <span class="label">模式</span>
      <label class="check">
        <input type="radio" name="mode" value="flac" bind:group={mode} />
        上传 FLAC
      </label>
      <div class="row wrap">
        <label class="check">
          <input type="radio" name="mode" value="transcodes" bind:group={mode} />
          仅上传转码版本到专辑组
        </label>
        {#if transcodes}
          <input type="text" class="inline" bind:value={groupId} placeholder="ID或URL" aria-label="专辑组 ID" />
        {/if}
      </div>
      {#if transcodes}
        <small class="hint">
          适用于专辑组中已有 FLAC 的情况：只上传转码版本，需选择一个站点（--skip-flac-upload）。请选择至少一种转码格式，或勾选“运行时询问”。此模式不处理求种和上传后频谱。
        </small>
      {/if}
    </div>

    <details class="overrides">
      <summary>{summary}</summary>
      <div class="grid">
        {#if !transcodes}
          <label>
            专辑组 ID
            <input type="text" bind:value={groupId} placeholder="新建专辑组" />
            {#if showHelp}<small class="hint">上传到该组：ID或torrents.php?id=链接（-g)。</small>{/if}
          </label>
        {/if}
        {#if !transcodes}
          <label>
            求种
            <input type="text" bind:value={request} placeholder="无" />
            {#if showHelp}<small class="hint">要完成的求种：填写求种链接或 ID（-r）。</small>{/if}
          </label>
        {/if}
        <label>
          有损母带
          <select bind:value={lossy}>
            <option value="ask">检查后询问</option>
            <option value="yes">是</option>
            <option value="no">否</option>
          </select>
          {#if showHelp}<small class="hint">母带是否为有损来源（-l / -L）。</small>{/if}
        </label>
        <label>
          编码
          <select bind:value={encoding}>
            <option value="">无损，还是问</option>
            {#each options.encodings as each (each)}<option value={each}>{each}</option>{/each}
          </select>
          {#if showHelp}<small class="hint">仅适用于有损文件（-e)。</small>{/if}
        </label>
        <label>
          频谱曲目
          <input type="text" bind:value={spectrals} placeholder="运行时询问" />
          {#if showHelp}<small class="hint">要在描述中展示频谱的曲目编号，例如1 4 7 (-sp)。</small>{/if}
        </label>
      </div>

      <div class="opts" class:helped={showHelp}>
        {#if sourceUrl.trim()}
          <div class="opt">
            <label class="check">
              <input type="checkbox" bind:checked={searchAnyway} />
              无论如何搜索商店
            </label>
            {#if showHelp}
              <small class="hint">此时，发行页面链接只写入描述，相当于单独使用 -su。</small>
            {/if}
          </div>
        {/if}
        {#each shownFlags as flag (flag.key)}
          <div class="opt">
            <label class="check" title={flag.help}>
              <input type="checkbox" bind:checked={flags[flag.key]} />
              {flag.label}
            </label>
            {#if showHelp}<small class="hint">{flag.help}</small>{/if}
          </div>
        {/each}
      </div>
    </details>

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
      <button class="link" onclick={resetOptions}>重置为默认值</button>
      <label class="check help">
        <input type="checkbox" bind:checked={showHelp} /> 解释选项
      </label>
    </div>
    {#if showHelp}
      <p class="hint">
        模拟运行会显示原本要提交的上传表单，不会提交上传。-yyy 自动采用是非题及频谱曲目的默认值；专辑组、元数据结果、编辑字段、来源介质、编码和异常处理仍需人工确认。上传后频谱涉及的有损母带、频谱 ID 和审批问题也仍会询问。
      </p>
    {/if}

    <div class="row actions">
      <button class="btn" onclick={start} disabled={!canStart}>
        {dryRun ? "开始模拟运行" : "开始上传"}
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
      {:else if job.dry_run}
        <p class="muted">模拟运行已完成，未提交上传。预览表单可在日志中查看。</p>
      {:else}
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
    grid-template-columns: repeat(auto-fit, minmax(min(220px, 100%), 1fr));
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
  .opts {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem 1.1rem;
    margin-top: 0.9rem;
  }
  .opts.helped {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(260px, 100%), 1fr));
    gap: 0.6rem 1.2rem;
  }
  .mode {
    margin-top: 0.9rem;
  }
  .inline {
    width: 14rem;
  }
  .overrides {
    margin-top: 0.9rem;
  }
  .overrides summary {
    cursor: pointer;
    font-size: 0.85rem;
    color: var(--text-dim);
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
  .link {
    background: none;
    border: none;
    padding: 0;
    color: var(--text-dim);
    font: inherit;
    font-size: 0.85rem;
    text-decoration: underline;
    cursor: pointer;
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
