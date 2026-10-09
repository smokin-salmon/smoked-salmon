<script lang="ts">
  import { apiPost } from '../lib/api'
  import FolderPicker from '../lib/FolderPicker.svelte'
  import JobActivity from '../lib/JobActivity.svelte'
  import JobStatus from '../lib/JobStatus.svelte'
  import VerdictRows from '../lib/VerdictRows.svelte'
  import type { ChecksResult } from '../lib/verdicts'
  import { FINISHED, jobStore, type Job } from '../lib/jobs.svelte'

  let path = $state('')
  let report = $state(false)
  let error = $state('')
  let starting = $state(false)
  let jobId = $state<string | null>(null)
  let showLog = $state(false)
  let copied = $state(false)

  // The job started here, else the newest checks job.
  const job = $derived(
    (jobId ? jobStore.get(jobId) : undefined) ?? jobStore.jobs.find((j) => j.kind === 'checks'),
  )
  const result = $derived(job?.status === 'done' ? (job.result as ChecksResult | null) : null)

  async function run() {
    error = ''
    starting = true
    try {
      const started = await apiPost<Job>('/jobs', { kind: 'checks', params: { path, report } })
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

  async function copyReport() {
    if (!result?.report) return
    try {
      await navigator.clipboard.writeText(result.report)
      copied = true
      setTimeout(() => (copied = false), 1500)
    } catch (e) {
      error = `Could not copy the report: ${e}`
    }
  }
</script>

<!-- Ported from the fork's Checks page (chodeus, 9bfdddc3), on salmon check all's rows. -->
<h1>Checks</h1>
<p class="lead">
  Every check <span class="mono">salmon check all</span> runs on an album folder: source, integrity, MQA, upconverts,
  rip logs, tags, sample rate, path length, provenance, the frequency analysis and the Do-Not-Upload lists. No tracker
  is contacted, and nothing in the folder is changed. Advisory only: <span class="mono">salmon up</span> runs its own
  checks.
</p>

<div class="card">
  <FolderPicker bind:value={path} />
  <div class="row actions">
    <label class="row option">
      <input type="checkbox" bind:checked={report} />
      Also a plain-text report to paste into a help thread
    </label>
  </div>
  <div class="row actions">
    <button class="btn" onclick={run} disabled={!path || starting}>Run checks</button>
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

    {#if result}
      <p class="summary">
        {#if result.blocking}
          <span class="chip err">{result.blocking} blocking</span>
        {/if}
        {#if result.warnings}
          <span class="chip warn">{result.warnings} warning(s)</span>
        {/if}
        {#if !result.blocking && !result.warnings}
          <span class="chip ok">Nothing blocking, no warning</span>
        {/if}
      </p>
      <VerdictRows rows={result.rows} />
      {#if result.report}
        <details class="report">
          <summary>Report</summary>
          <pre class="mono">{result.report}</pre>
          <button class="btn small secondary" onclick={copyReport}>{copied ? 'Copied' : 'Copy report'}</button>
        </details>
      {/if}
    {/if}

    {#if showLog || !FINISHED.includes(job.status)}
      <JobActivity {job} logTail={showLog ? 0 : 15} />
    {/if}
  </div>
{/if}

<style>
  .actions {
    margin-top: 0.7rem;
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
  .summary {
    display: flex;
    gap: 0.4rem;
    margin: 0.8rem 0 0;
  }
  .report {
    margin-top: 0.8rem;
  }
  .report summary {
    cursor: pointer;
    color: var(--text-dim);
  }
  .report pre {
    white-space: pre-wrap;
    background: var(--bg);
    border-radius: 8px;
    padding: 0.7rem;
    font-size: 0.78rem;
    overflow-wrap: anywhere;
  }
</style>
