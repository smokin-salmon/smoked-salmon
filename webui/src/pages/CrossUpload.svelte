<script lang="ts">
  import { apiGet, apiPost } from '../lib/api'
  import FolderPicker from '../lib/FolderPicker.svelte'
  import JobActivity from '../lib/JobActivity.svelte'
  import JobStatus from '../lib/JobStatus.svelte'
  import QuestionPanel from '../lib/QuestionPanel.svelte'
  import { FINISHED, jobStore, type Job } from '../lib/jobs.svelte'

  interface Options {
    trackers: string[]
    transcodes: string[]
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
  }

  let options = $state<Options>({ trackers: [], transcodes: [], max_releases: 5 })

  let inputs = $state('')
  let source = $state('')
  let target = $state('')
  let path = $state('')
  let groupId = $state('')
  let transcodes = $state<string[]>([])
  let downconvert = $state(false)
  let allFormats = $state(false)
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
        error = `Could not load the trackers: ${e}`
      })
  })

  function toggleTranscode(bitrate: string) {
    transcodes = transcodes.includes(bitrate) ? transcodes.filter((b) => b !== bitrate) : [...transcodes, bitrate]
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
      error = `The ${target} group ID must be a number or a torrents.php?id= link.`
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
          transcodes,
          downconvert,
          all_formats: allFormats,
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
<h1>Cross-upload</h1>
<p class="lead">
  What <span class="mono">salmon cross-upload</span> does, here: read each torrent from one tracker, check the files
  on disk against it, show the plan, then upload it to the other tracker with the same questions and the same requests.
  A dry run reads from both trackers and sends nothing.
</p>

{#if !live}
  <div class="card">
    <label>
      Torrents
      <input
        type="text"
        class="mono"
        bind:value={inputs}
        placeholder="torrent IDs or torrent URLs, separated by spaces"
      />
      {#if given.length > options.max_releases}
        <small class="hint warn">At most {options.max_releases} per job.</small>
      {:else if showHelp}
        <small class="hint">
          Each a {source || 'source'} torrent ID or a torrents.php?torrentid= link, at most {options.max_releases}.
        </small>
      {/if}
    </label>

    <div class="grid">
      <label>
        From
        <select bind:value={source}>
          {#each options.trackers as code (code)}<option value={code}>{code}</option>{/each}
        </select>
        {#if showHelp}<small class="hint">The tracker the torrents are on (SOURCE_TRACKER).</small>{/if}
      </label>
      <label>
        To
        <select bind:value={target}>
          {#each options.trackers as code (code)}<option value={code}>{code}</option>{/each}
        </select>
        {#if showHelp}<small class="hint">The tracker to upload them to (TARGET_TRACKER).</small>{/if}
      </label>
      <label>
        Group ID
        <input type="text" bind:value={groupId} placeholder="find it, or a new group" />
        {#if showHelp}
          <small class="hint">Upload into this {target || 'target'} group: an ID or a torrents.php?id= link (-g).</small>
        {/if}
      </label>
    </div>

    <div class="field folder">
      <span class="label">Album folder (optional)</span>
      <FolderPicker bind:value={path} />
      {#if showHelp}
        <small class="hint">
          Only when the files are not in download_directory under the torrent's folder name (--path). One torrent
          only.
        </small>
      {/if}
    </div>

    <div class="opts">
      {#each options.transcodes as bitrate (bitrate)}
        <label class="check" title="Also upload this MP3 transcode of a FLAC (--transcode {bitrate}).">
          <input type="checkbox" checked={transcodes.includes(bitrate)} onchange={() => toggleTranscode(bitrate)} />
          MP3 {bitrate}
        </label>
      {/each}
      <label class="check" title="Also upload the lossless downconversions of a 24-bit FLAC (--downconvert).">
        <input type="checkbox" bind:checked={downconvert} /> Downconversions
      </label>
      <label class="check" title="Also upload every conversion salmon up would offer (--all).">
        <input type="checkbox" bind:checked={allFormats} /> All formats
      </label>
    </div>
    {#if showHelp}
      <p class="hint">
        The conversions go into the same group, each made from the files on disk, as salmon up makes them. A format
        the group already has in this edition is left out.
      </p>
    {/if}

    <div class="row wrap chips">
      <button class="chip toggle" class:on={dryRun} aria-pressed={dryRun} onclick={() => (dryRun = !dryRun)}>
        dry run
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
        <input type="checkbox" bind:checked={showHelp} /> Explain the options
      </label>
    </div>
    {#if showHelp}
      <p class="hint">
        A dry run sends nothing: each upload's form is printed instead. -yyy answers the yes or no questions with their
        defaults, as in the terminal; the questions with no default are still asked.
      </p>
    {/if}

    <div class="row actions">
      <button
        class="btn"
        onclick={start}
        disabled={!given.length || given.length > options.max_releases || !source || !target || starting}
      >
        {dryRun ? 'Start the dry run' : 'Start the cross-upload'}
      </button>
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
      {#if result.uploads.length}
        {#if job.status !== 'done'}<p class="muted">Uploaded before it stopped:</p>{/if}
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
        <p class="muted">Dry run: nothing was sent. The forms it would have sent are in the log.</p>
      {:else if job.status === 'done'}
        <p class="muted">Nothing was uploaded.</p>
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
  .opts {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem 1.1rem;
    margin-top: 0.9rem;
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
