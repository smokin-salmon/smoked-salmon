<script lang="ts">
  import { apiGet, apiPost } from '../lib/api'
  import FolderPicker from '../lib/FolderPicker.svelte'
  import JobActivity from '../lib/JobActivity.svelte'
  import JobStatus from '../lib/JobStatus.svelte'
  import QuestionPanel from '../lib/QuestionPanel.svelte'
  import { FINISHED, jobStore, type Job } from '../lib/jobs.svelte'

  interface Options {
    sources: string[]
    encodings: string[]
  }

  interface TagResult {
    folder: string
  }

  let options = $state<Options>({ sources: [], encodings: [] })

  let path = $state('')
  let source = $state('')
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
        error = `Could not load the sources: ${e}`
      })
  })

  async function start() {
    error = ''
    starting = true
    try {
      const started = await apiPost<Job>('/jobs', {
        kind: 'tag',
        params: {
          path,
          source,
          encoding: encoding || null,
          overwrite,
          auto_rename: autoRename,
          skip_initial_review: skipInitialReview,
          apply_ai_suggestions: applyAiSuggestions,
        },
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

<!-- Ported from the fork's tag editor in Tools (chodeus, 0b29d2d5, a18e0e25), on the job of salmon tag. -->
<h1>Tag</h1>
<p class="lead">
  What <span class="mono">salmon tag</span> does, here: look up the album's metadata, review it, retag the files and
  rename them, without uploading. The job asks the questions the command asks, in the same order. No tracker is
  contacted. An album in <span class="mono">library_dirs</span> is tagged as a copy renamed into
  <span class="mono">download_directory</span>; the album itself is never changed.
</p>

{#if !live}
  <div class="card">
    <FolderPicker bind:value={path} />

    <div class="grid">
      <label>
        Source
        <select bind:value={source}>
          <option value="" disabled>Pick one</option>
          {#each options.sources as each (each)}<option value={each}>{each}</option>{/each}
        </select>
        <small class="hint">The media the files came from (<span class="mono">-s</span>).</small>
      </label>
      <label>
        Encoding
        <select bind:value={encoding}>
          <option value="">lossless, or ask</option>
          {#each options.encodings as each (each)}<option value={each}>{each}</option>{/each}
        </select>
        <small class="hint">For lossy files only (<span class="mono">-e</span>).</small>
      </label>
    </div>

    <div class="opts">
      <label class="check" title="Rename files and folders without asking first (-n).">
        <input type="checkbox" bind:checked={autoRename} /> Auto-rename
      </label>
      <label
        class="check"
        title="Take artists, year, label, catalogue number and genres from the scraped sources, not the file tags (-ow)."
      >
        <input type="checkbox" bind:checked={overwrite} /> Overwrite metadata
      </label>
      <label
        class="check"
        title="Skip the manual metadata review before the AI review (--skip-initial-review). Only with upload.ai_review on."
      >
        <input type="checkbox" bind:checked={skipInitialReview} /> Skip initial review
      </label>
      <label
        class="check"
        title="Apply the AI review's edits without asking (--apply-ai-suggestions). Only with upload.ai_review on."
      >
        <input type="checkbox" bind:checked={applyAiSuggestions} /> Apply AI suggestions
      </label>
      <button
        class="chip toggle"
        class:on={assumeDefaults}
        aria-pressed={assumeDefaults}
        title="Answer the yes or no questions with their defaults, as in the terminal; the questions with no default are still asked."
        onclick={() => (assumeDefaults = !assumeDefaults)}
      >
        -yyy
      </button>
    </div>

    <div class="row actions">
      <button class="btn" onclick={start} disabled={!path || !source || starting}>Tag the album</button>
    </div>
    {#if error}<p class="error">{error}</p>{/if}
  </div>
{/if}

{#if job}
  <div class="card">
    <div class="row head">
      <h2 class="grow">{job.title}</h2>
      {#if live}
        <button class="btn small secondary" onclick={cancel}>Cancel</button>
      {:else}
        <button class="btn small secondary" onclick={toggleLog}>{showLog ? 'Hide log' : 'Log'}</button>
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
