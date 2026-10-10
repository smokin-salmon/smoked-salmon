<script lang="ts">
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
<h1>Convert</h1>
<p class="lead">
  Transcode an album to MP3 (<span class="mono">salmon transcode</span>), downconvert 24-bit FLACs to 16-bit
  (<span class="mono">salmon downconv</span>), or recompress FLACs in place (<span class="mono">salmon compress</span>).
  No tracker is contacted. A conversion writes a new folder beside the album, or for an album in
  <span class="mono">library_dirs</span> into <span class="mono">download_directory</span>; the album itself is never
  changed. Recompress changes the album's files, so it is refused for an album in <span class="mono">library_dirs</span>.
</p>

<div class="card">
  <FolderPicker bind:value={path} />
  <div class="row actions">
    <select bind:value={bitrate} class="bitrate" aria-label="MP3 bitrate">
      <option value="V0">MP3 V0</option>
      <option value="320">MP3 320</option>
    </select>
    <label class="row option">
      <input type="checkbox" bind:checked={essentialOnly} />
      Essential files only (<span class="mono">-eo</span>): audio and images, no cue, log or other extras
    </label>
  </div>
  <div class="row actions">
    <button class="btn" onclick={() => run('transcode')} disabled={!path || starting}>Transcode</button>
    <button class="btn secondary" onclick={() => run('downconvert')} disabled={!path || starting}>
      Downconvert to 16-bit
    </button>
    <button class="btn secondary" onclick={() => run('compress')} disabled={!path || starting}>
      Recompress FLACs in place
    </button>
  </div>
  {#if error}<p class="error">{error}</p>{/if}
</div>

{#if job}
  <div class="card">
    <div class="row head">
      <h2 class="grow">{job.title}</h2>
      <button class="btn small secondary" onclick={toggleLog}>{showLog ? 'Hide log' : 'Log'}</button>
    </div>
    <JobStatus {job} />

    {#if result?.output}
      <p class="mono muted result">→ {result.output}</p>
    {:else if result && result.recompressed !== undefined}
      <p class="mono muted result">Recompressed {result.recompressed} FLAC(s) in place.</p>
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
