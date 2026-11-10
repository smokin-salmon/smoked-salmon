<script lang="ts">
  import { t } from '../lib/i18n'
  import { apiPost } from '../lib/api'
  import FolderPicker from '../lib/FolderPicker.svelte'
  import JobActivity from '../lib/JobActivity.svelte'
  import JobStatus from '../lib/JobStatus.svelte'
  import { FINISHED, jobStore, type Job } from '../lib/jobs.svelte'

  type Action = 'transcode' | 'downconvert' | 'compress'

  const KINDS: Action[] = ['transcode', 'downconvert', 'compress']

  interface ConvertResult {
    output?: string
    folder?: string
    recompressed?: number
  }

  let path = $state('')
  // Whether the picker has checked the folder and a job may start on it.
  let usable = $state(false)
  let bitrate = $state<'V0' | '320'>('V0')
  let essentialOnly = $state(false)
  let error = $state('')
  let starting = $state(false)
  let jobId = $state<string | null>(null)
  let showLog = $state(false)

  // The job started here, else the newest conversion.
  const job = $derived(
    (jobId ? jobStore.get(jobId) : undefined) ?? jobStore.jobs.find((j) => KINDS.includes(j.kind as Action)),
  )
  const result = $derived(job?.status === 'done' ? (job.result as ConvertResult | null) : null)

  async function run(kind: Action) {
    error = ''
    starting = true
    try {
      const params =
        kind === 'transcode'
          ? { path, bitrate, essential_only: essentialOnly }
          : kind === 'downconvert'
            ? { path, essential_only: essentialOnly }
            : { path }
      const started = await apiPost<Job>('/jobs', { kind, params })
      jobId = started.id
      showLog = false
    } catch (e) {
      error = String(e)
    } finally {
      starting = false
    }
  }

  function toggleLog() {
    showLog = !showLog
    if (showLog && job) void jobStore.show(job.id)
  }
</script>

<!-- Ported from the fork's Convert page (chodeus, 0b29d2d5 and a18e0e25), on the jobs of salmon transcode,
     salmon downconv and salmon compress. -->
<h1>转码</h1>
<p class="lead">
  将专辑转为 MP3（<span class="mono">salmon transcode</span>）、将 24 位 FLAC 降为 16 位（<span class="mono">salmon downconv</span>），或重新压缩 FLAC（<span class="mono">salmon compress</span>）。转码结果保存在专辑旁的新文件夹中；如果原专辑位于 <span class="mono">library_dirs</span>，则保存到 <span class="mono">download_directory</span>。重新压缩会直接修改原文件，因此不允许对媒体库中的专辑执行。
</p>

<div class="card">
  <FolderPicker bind:value={path} bind:usable />
  <div class="row actions">
    <select bind:value={bitrate} class="bitrate" aria-label="MP3比特率">
      <option value="V0">MP3 V0</option>
      <option value="320">MP3 320</option>
    </select>
    <label class="row option">
      <input type="checkbox" bind:checked={essentialOnly} />
      仅重要文件（<span class="mono">-eo</span>):音频和图像，没有提示、日志或其他附加内容
    </label>
  </div>
  <div class="row actions">
    <button class="btn" onclick={() => run('transcode')} disabled={!usable || starting}>转码</button>
    <button class="btn secondary" onclick={() => run('downconvert')} disabled={!usable || starting}>
      降位深至 16-bit
    </button>
    <button class="btn secondary" onclick={() => run('compress')} disabled={!usable || starting}>
      重新压缩原位置的 FLAC 文件
    </button>
  </div>
  {#if error}<p class="error">{error}</p>{/if}
</div>

{#if job}
  <div class="card">
    <div class="row head">
      <h2 class="grow">{t(job.title)}</h2>
      <button class="btn small secondary" onclick={toggleLog}>{showLog ? "隐藏日志" : "日志"}</button>
    </div>
    <JobStatus {job} />

    {#if result?.output}
      <p class="mono muted result">→ {result.output}</p>
    {:else if result && result.recompressed !== undefined}
      <p class="mono muted result">重新压缩 {result.recompressed} FLAC就位。</p>
    {/if}

    {#if showLog || !FINISHED.includes(job.status)}
      <JobActivity {job} logTail={showLog ? 0 : 15} />
    {/if}
  </div>
{/if}

<style>
  .actions {
    margin-top: 0.7rem;
    flex-wrap: wrap;
  }
  .bitrate {
    width: auto;
  }
  .option {
    gap: 0.4rem;
    font-size: 0.9rem;
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
  .result {
    overflow-wrap: anywhere;
  }
</style>
