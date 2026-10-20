<script lang="ts">
  import { apiGet, apiPost } from '../lib/api'
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
  }

  /** The trackers ticked when nothing is remembered: the config's default_trackers, else the first configured. */
  function firstVisitTrackers(answer: Options): string[] {
    const defaults = (answer.default_trackers ?? []).filter((t) => answer.trackers.includes(t))
    if (defaults.length) return defaults
    return answer.trackers.length ? [answer.trackers[0]] : []
  }

  let options = $state<Options>({ trackers: [], default_trackers: [], sources: [], encodings: [] })

  let path = $state('')
  let chosen = $state<string[]>([])
  let source = $state('')
  let lossy = $state<'ask' | 'yes' | 'no'>('ask')
  let groupId = $state('')
  let request = $state('')
  let sourceUrl = $state('')
  let encoding = $state('')
  let spectrals = $state('')
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
    { key: 'auto_rename', label: 'Auto-rename', help: 'Rename files and folders without asking first (-n).' },
    {
      key: 'spectrals_after',
      label: 'Spectrals after upload',
      help: 'Check, upload and report the spectrals after the torrent is up (-a). Not with a dry run.',
    },
    {
      key: 'compress',
      label: 'Recompress FLACs',
      help: 'Recompress the FLACs to the configured compression level first (-c). The audio is unchanged.',
    },
    {
      key: 'scene',
      label: 'Scene release',
      help: 'Leave the folder and file names, tags and cover as they are (--scene). Not with essential files only.',
    },
    {
      key: 'essential_only',
      label: 'Essential files only',
      help: 'Upload audio, logs, cues and artwork only; leave out nfo, sfv, md5, txt and other extras (-eo).',
    },
    {
      key: 'skip_flac_upload',
      label: 'Transcodes only',
      help: 'The FLAC is already in the Group ID group: upload only transcodes of it, into that group (--skip-flac-upload). Needs a group ID and one tracker; not with a request or spectrals after upload.',
    },
    {
      key: 'overwrite',
      label: 'Overwrite metadata',
      help: "Take artists, year, label, catalogue number and genres from the scraped sources, not the file tags (-ow).",
    },
    { key: 'skip_up', label: 'Skip upconvert check', help: 'Skip the check for 16-bit audio padded to 24-bit (--skip-up).' },
    { key: 'skip_mqa', label: 'Skip MQA check', help: 'Skip the check for an MQA marker (--skip-mqa).' },
    { key: 'skip_log_check', label: 'Skip log check', help: 'Skip scoring CD rip logs (--skip-log-check).' },
    {
      key: 'skip_integrity_check',
      label: 'Skip integrity check',
      help: 'Skip checking that every audio file decodes (--skip-integrity-check).',
    },
    {
      key: 'skip_initial_review',
      label: 'Skip initial review',
      help: 'Skip the manual metadata review before the AI review (--skip-initial-review). Only with upload.ai_review on.',
    },
    {
      key: 'apply_ai_suggestions',
      label: 'Apply AI suggestions',
      help: "Apply the AI review's edits without asking (--apply-ai-suggestions). Only with upload.ai_review on.",
    },
  ]

  // The job started here, else the newest upload: back on the page, the running upload is shown again.
  // Ported from the fork's Upload page (chodeus, 889cc4f5).
  const job = $derived(
    (jobId ? jobStore.get(jobId) : undefined) ?? jobStore.jobs.find((j) => j.kind === 'upload'),
  )
  const live = $derived(job !== undefined && !FINISHED.includes(job.status))
  const result = $derived(job?.status === 'done' ? (job.result as UploadResult | null) : null)

  $effect(() => {
    apiGet<Options>('/upload/options')
      .then((answer) => {
        options = answer
        if (chosen.length || !answer.trackers.length) return
        const kept = storedOptions().trackers
        const still = kept ? answer.trackers.filter((t) => kept.includes(t)) : []
        // A stored empty list stays empty; nothing stored, or all gone from the config: the first-visit preset.
        chosen = kept && (kept.length === 0 || still.length) ? still : firstVisitTrackers(answer)
      })
      .catch((e) => {
        error = `Could not load the trackers: ${e}`
      })
  })

  // Kept in the order the config lists them, which is the order the job uploads in.
  function toggleTracker(code: string) {
    chosen = chosen.includes(code)
      ? chosen.filter((c) => c !== code)
      : options.trackers.filter((t) => t === code || chosen.includes(t))
  }

  // Exactly the config's default_trackers, in its order: sent as none, so the job runs as salmon up without -t
  // (the other trackers offered after them, Transcodes only on the first) rather than as -t.
  const asDefaults = $derived(
    options.default_trackers.length > 0 &&
      chosen.length === options.default_trackers.length &&
      chosen.every((code, i) => code === options.default_trackers[i]),
  )

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
      error = 'The group ID must be a number or a torrents.php?id= link.'
      return
    }
    const tracks = spectrals.split(/[,\s]+/).filter(Boolean).map(Number)
    if (tracks.some((n) => !Number.isInteger(n) || n < 1)) {
      error = 'Spectral track numbers are whole numbers from 1.'
      return
    }
    saveOptions()
    starting = true
    try {
      const started = await apiPost<Job>('/jobs', {
        kind: 'upload',
        params: {
          path,
          source,
          trackers: asDefaults ? [] : chosen,
          group_id: group,
          request: request.trim() || null,
          source_url: sourceUrl.trim() || null,
          encoding: encoding || null,
          lossy: lossy === 'ask' ? null : lossy === 'yes',
          spectrals: tracks,
          ...flags,
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

<!-- Ported from the fork's Upload page (styx-techno 637ee666; chodeus 0b29d2d5, 889cc4f5, 6f846b37), without its
     pre-flight step: the job asks what salmon up asks, in the order it asks it. -->
<h1>Upload</h1>
<p class="lead">
  What <span class="mono">salmon up</span> does, here: the same checks, the same questions as the job reaches them,
  and the same requests to the tracker. A dry run goes through everything on a copy of the album and sends nothing.
</p>

{#if !live}
  <div class="card">
    <FolderPicker bind:value={path} />

    <div class="grid">
      <div class="field">
        <span class="label">Trackers, in order</span>
        <div class="row wrap">
          {#each options.trackers as code (code)}
            <label class="check">
              <input type="checkbox" checked={chosen.includes(code)} onchange={() => toggleTracker(code)} />
              {code}
            </label>
          {/each}
        </div>
        {#if !chosen.length}
          <small class="hint">
            None: as salmon up without -t, {options.default_trackers?.length
              ? `${options.default_trackers.join(', ')} from your config`
              : 'the job asks which'}.
          </small>
        {:else if showHelp && asDefaults}
          <small class="hint">
            Your config's default_trackers: uploads to each in turn, then offers the others (multi_tracker_upload),
            as salmon up without -t.
          </small>
        {:else if showHelp}
          <small class="hint">Uploads to each in turn, and asks about no other tracker (-t).</small>
        {/if}
      </div>
      <label>
        Source
        <select bind:value={source}>
          <option value="" disabled>Pick one</option>
          {#each options.sources as each (each)}<option value={each}>{each}</option>{/each}
        </select>
        {#if showHelp}<small class="hint">The media the files came from (-s).</small>{/if}
      </label>
      <label>
        Lossy master
        <select bind:value={lossy}>
          <option value="ask">check, and ask</option>
          <option value="yes">yes</option>
          <option value="no">no</option>
        </select>
        {#if showHelp}<small class="hint">Whether the master is lossy (-l / -L).</small>{/if}
      </label>
      <label>
        Group ID
        <input type="text" bind:value={groupId} placeholder="a new group" />
        {#if showHelp}<small class="hint">Upload into this group: an ID or a torrents.php?id= link (-g).</small>{/if}
      </label>
      <label>
        Request
        <input type="text" bind:value={request} placeholder="none" />
        {#if showHelp}<small class="hint">The request to fill: a link or an ID (-r).</small>{/if}
      </label>
      <label>
        Source URL
        <input type="text" bind:value={sourceUrl} placeholder="https://" />
        {#if showHelp}<small class="hint">Where a WEB release came from, for its description (-su).</small>{/if}
      </label>
      <label>
        Encoding
        <select bind:value={encoding}>
          <option value="">lossless, or ask</option>
          {#each options.encodings as each (each)}<option value={each}>{each}</option>{/each}
        </select>
        {#if showHelp}<small class="hint">For lossy files only (-e).</small>{/if}
      </label>
      <label>
        Spectral tracks
        <input type="text" bind:value={spectrals} placeholder="ask" />
        {#if showHelp}<small class="hint">The track numbers whose spectrals go in the description, e.g. 1 4 7 (-sp).</small>{/if}
      </label>
    </div>

    <div class="opts" class:helped={showHelp}>
      {#each FLAGS as flag (flag.key)}
        <div class="opt">
          <label class="check" title={flag.help}>
            <input type="checkbox" bind:checked={flags[flag.key]} />
            {flag.label}
          </label>
          {#if showHelp}<small class="hint">{flag.help}</small>{/if}
        </div>
      {/each}
    </div>

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
      <button class="link" onclick={resetOptions}>Reset to defaults</button>
      <label class="check help">
        <input type="checkbox" bind:checked={showHelp} /> Explain the options
      </label>
    </div>
    {#if showHelp}
      <p class="hint">
        A dry run changes nothing and sends nothing: each upload's form is printed instead. -yyy answers the yes or no
        questions with their defaults, as in the terminal; the questions with no default are still asked.
      </p>
    {/if}

    <div class="row actions">
      <button class="btn" onclick={start} disabled={!path || !source || starting}>
        {dryRun ? 'Start the dry run' : 'Start the upload'}
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
        <p class="muted">Dry run: nothing was sent. The forms it would have sent are in the log.</p>
      {:else}
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
