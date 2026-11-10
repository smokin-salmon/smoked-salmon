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
    sources: string[]
    encodings: string[]
    ai_review: boolean
  }

  interface TagResult {
    folder: string
  }

  let options = $state<Options>({ sources: [], encodings: [], ai_review: false })

  let path = $state('')
  // Whether the picker has checked the folder and a job may start on it.
  let usable = $state(false)
  let source = $state('')
  const preset = sourcePreset(
    () => path,
    (found) => (source = found),
  )
  let encoding = $state('')
  let overwrite = $state(false)
  let autoRename = $state(false)
  let skipInitialReview = $state(false)
  let applyAiSuggestions = $state(false)
  let assumeDefaults = $state(false)

  let error = $state('')
  let starting = $state(false)
  let jobId = $state<string | null>(null)
  let showLog = $state(false)

  // The job started here, else the newest tag job: back on the page, the running job is shown again.
  const job = $derived((jobId ? jobStore.get(jobId) : undefined) ?? jobStore.jobs.find((j) => j.kind === 'tag'))
  const live = $derived(job !== undefined && !FINISHED.includes(job.status))
  const result = $derived(job?.status === 'done' ? (job.result as TagResult | null) : null)

  $effect(() => {
    apiGet<Options>('/upload/options')
      .then((answer) => {
        options = answer
      })
      .catch((e) => {
        error = `无法加载源： ${e}`
      })
  })

  async function start() {
    error = ''
    starting = true
    const sent = path
    try {
      const started = await apiPost<Job>('/jobs', {
        kind: 'tag',
        params: {
          path: sent,
          source,
          encoding: encoding || null,
          overwrite,
          auto_rename: autoRename,
          skip_initial_review: options.ai_review && skipInitialReview,
          apply_ai_suggestions: options.ai_review && applyAiSuggestions,
        },
        assume_defaults: assumeDefaults,
      })
      jobId = started.id
      showLog = false
      // The job renames the folder: the next one starts with nothing chosen, unless another was typed meanwhile.
      if (path === sent) path = ''
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

<!-- Ported from the fork's tag editor in Tools (chodeus, 0b29d2d5, a18e0e25), on the job of salmon tag. -->
<h1>标签整理</h1>
<p class="lead">
  执行 <span class="mono">salmon tag</span>，查找并核对专辑元数据，写入标签并重命名文件。运行时会按步骤询问，无需上传到站点。位于 <span class="mono">library_dirs</span> 的专辑会先复制到 <span class="mono">download_directory</span>，再修改副本。
</p>

{#if !live}
  <div class="card">
    <FolderPicker bind:value={path} bind:usable />

    <div class="grid">
      <label>
        来源介质
        <select bind:value={source} onchange={() => preset.picked()}>
          <option value="" disabled>选择一个</option>
          {#each options.sources as each (each)}<option value={each}>{each}</option>{/each}
        </select>
        {#if preset.reason}<small class="hint muted">{t(preset.reason)}</small>{/if}
        <small class="hint">文件的来源介质（<span class="mono">-s</span>).</small>
      </label>
      <label>
        编码
        <select bind:value={encoding}>
          <option value="">无损，或运行时询问</option>
          {#each options.encodings as each (each)}<option value={each}>{each}</option>{/each}
        </select>
        <small class="hint">仅适用于有损文件（<span class="mono">-e</span>).</small>
      </label>
    </div>

    <div class="opts">
      <label class="check" title="在不首先询问的情况下重命名文件和文件夹(-n)。">
        <input type="checkbox" bind:checked={autoRename} /> 自动重命名
      </label>
      <label
        class="check"
        title="使用抓取结果中的艺术家、年份、厂牌、发行编号和流派覆盖文件标签（-ow）。"
      >
        <input type="checkbox" bind:checked={overwrite} /> 覆盖元数据
      </label>
      {#if options.ai_review}
      <label
        class="check"
        title="在AI审查之前跳过手动元数据审查(--skip-initial-review)。仅当 upload.ai_review 打开时。"
      >
        <input type="checkbox" bind:checked={skipInitialReview} /> 跳过首次人工核对
      </label>
      <label
        class="check"
        title="无需询问即可应用AI审阅的编辑（--apply-ai-suggestions）。仅当 upload.ai_review 打开时。"
      >
        <input type="checkbox" bind:checked={applyAiSuggestions} /> 应用 AI 修改建议
      </label>
      {/if}
      <button
        class="chip toggle"
        class:on={assumeDefaults}
        aria-pressed={assumeDefaults}
        title="使用默认值回答是或否问题，如在终端中一样；仍然会问没有默认的问题。"
        onclick={() => (assumeDefaults = !assumeDefaults)}
      >
        -yyy
      </button>
    </div>

    <div class="row actions">
      <button class="btn" onclick={start} disabled={!usable || !source || starting}>标记专辑</button>
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
      <p class="mono muted folder">→ {result.folder}</p>
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
  label {
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
  .hint {
    display: block;
    color: var(--text-dim);
    font-size: 0.78rem;
    line-height: 1.35;
  }
  .opts {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem 1.1rem;
    margin-top: 0.9rem;
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
  .folder {
    overflow-wrap: anywhere;
  }
</style>
