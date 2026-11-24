<script lang="ts">
  import { ApiError, apiGet, apiPost } from '../lib/api'
  import { checksOf, problems } from '../lib/connectionCheck'
  import { FINISHED, jobStore, type Job } from '../lib/jobs.svelte'
  import { sentInClear } from '../lib/transport'

  interface Shown {
    set: boolean
    ends_with: string | null
  }
  interface TrackerSettings {
    code: string
    session: Shown
    /** null for a tracker that takes no API key (DIC). */
    api_key: Shown | null
  }
  interface Answer {
    path: string
    writable: boolean
    error: string | null
    trackers: TrackerSettings[]
  }

  let answer = $state<Answer | null>(null)
  let loadError = $state('')
  // What is typed, per tracker: never filled from the server, which never sends a stored value.
  let sessions = $state<Record<string, string>>({})
  let keys = $state<Record<string, string>>({})
  let saving = $state<Record<string, boolean>>({})
  let saveErrors = $state<Record<string, string>>({})
  // The connection check each save started, per tracker.
  let checkIds = $state<Record<string, string>>({})
  // Counts each tracker's saves that went through: a check whose values a later save replaced shows nothing.
  const saveCount: Record<string, number> = {}
  // Over plain http to another machine, a save sends the cookie unencrypted: it takes a second, explicit click.
  const inClear = sentInClear()
  let confirming = $state<Record<string, boolean>>({})

  $effect(() => {
    apiGet<Answer>('/settings/trackers')
      .then((a) => (answer = a))
      .catch((e) => (loadError = String(e)))
  })

  function shown(value: Shown): string {
    if (!value.set) return 'not set'
    return value.ends_with ? `set, ends in …${value.ends_with}` : 'set'
  }

  async function save(code: string, body: { session?: string; api_key?: string | null }) {
    let count: number
    saving[code] = true
    saveErrors[code] = ''
    try {
      answer = await apiPost<Answer>(`/settings/trackers/${code}`, body)
      // Only what was sent, and only if it was not changed meanwhile: a cookie typed before Remove API key stays.
      if (body.session !== undefined && sessions[code]?.trim() === body.session) sessions[code] = ''
      if (typeof body.api_key === 'string' && keys[code]?.trim() === body.api_key) keys[code] = ''
      // The last check tested the old values: if the new one cannot start, its result must not stay on show.
      delete checkIds[code]
      count = saveCount[code] = (saveCount[code] ?? 0) + 1
    } catch (e) {
      saveErrors[code] = e instanceof ApiError ? e.message : String(e)
      return
    } finally {
      saving[code] = false
    }
    await check(code, count)
  }

  function saveTyped(code: string) {
    if (inClear && !confirming[code]) {
      confirming[code] = true
      return
    }
    confirming[code] = false
    const body: { session?: string; api_key?: string } = {}
    if (sessions[code]?.trim()) body.session = sessions[code].trim()
    if (keys[code]?.trim()) body.api_key = keys[code].trim()
    return save(code, body)
  }

  function clearKey(code: string) {
    if (confirm(`Remove ${code}'s API key from your config?`)) return save(code, { api_key: null })
  }

  // The same check as the Dashboard's button, for this tracker only: at most two requests, only after a save.
  async function check(code: string, count: number) {
    try {
      const job = await apiPost<Job>('/jobs', { kind: 'connection_check', params: { trackers: [code] } })
      if (saveCount[code] === count) checkIds[code] = job.id
    } catch (e) {
      if (saveCount[code] !== count) return
      saveErrors[code] =
        e instanceof ApiError && e.status === 409
          ? 'Saved. A connection check is already running: run it again from the Dashboard once it ends.'
          : `Saved, but the connection check did not start: ${String(e)}`
    }
  }

  function checkJob(code: string): Job | undefined {
    return checkIds[code] ? jobStore.jobs.find((j) => j.id === checkIds[code]) : undefined
  }
</script>

<h1>Settings</h1>
<p class="lead">
  Your trackers' session cookies and API keys. A save writes them to your config file and applies to every job started
  after it, with no restart.
</p>

{#if loadError}
  <div class="card"><p class="muted">Could not load the settings: {loadError}</p></div>
{:else if !answer}
  <p class="muted">Loading…</p>
{:else}
  {#if inClear}
    <div class="card">
      <p class="small warning">
        This page is not on HTTPS: a cookie or key you save here crosses the network unencrypted, readable by anyone
        on the way. Open salmon web through a reverse proxy that does TLS, or through an SSH tunnel to localhost, or
        edit the config file by hand. On a VPN that encrypts (WireGuard, Tailscale), saving here is safe.
      </p>
    </div>
  {/if}

  <div class="card">
    <p class="small">Config file: <span class="mono">{answer.path}</span></p>
    {#if !answer.writable}
      <p class="small warning">
        salmon cannot write this file. Edit it by hand: new jobs read the session cookie and API key from it when they
        start, so no restart is needed for those.
      </p>
    {/if}
    {#if answer.error}<p class="err-text small">{answer.error}</p>{/if}
    <p class="muted small">
      Only the session cookie and API key apply without a restart, to jobs started from now on. Any other change to the
      config file, adding or removing a tracker included, needs salmon web to restart.
    </p>
  </div>

  {#each answer.trackers as tracker (tracker.code)}
    {@const job = checkJob(tracker.code)}
    <div class="card">
      <h2>{tracker.code}</h2>
      <div class="fields">
        <label>
          <span>Session cookie <span class="chip {tracker.session.set ? 'ok' : ''}">{shown(tracker.session)}</span></span>
          <input
            type="password"
            autocomplete="off"
            placeholder="Paste a new session cookie"
            bind:value={sessions[tracker.code]}
            disabled={!answer.writable}
          />
        </label>
        {#if tracker.api_key}
          <label>
            <span>API key <span class="chip {tracker.api_key.set ? 'ok' : ''}">{shown(tracker.api_key)}</span></span>
            <input
              type="password"
              autocomplete="off"
              placeholder="Paste a new API key"
              bind:value={keys[tracker.code]}
              disabled={!answer.writable}
            />
          </label>
        {/if}
      </div>
      <div class="row actions">
        <button
          class="btn small"
          onclick={() => saveTyped(tracker.code)}
          disabled={!answer.writable || saving[tracker.code] || !(sessions[tracker.code]?.trim() || keys[tracker.code]?.trim())}
        >
          {saving[tracker.code] ? 'Saving…' : confirming[tracker.code] ? 'Save over HTTP anyway' : 'Save and check'}
        </button>
        {#if confirming[tracker.code]}
          <button class="btn secondary small" onclick={() => (confirming[tracker.code] = false)}>Cancel</button>
        {/if}
        {#if tracker.api_key?.set}
          <button
            class="btn secondary small"
            onclick={() => clearKey(tracker.code)}
            disabled={!answer.writable || saving[tracker.code]}
          >
            Remove API key
          </button>
        {/if}
      </div>
      {#if confirming[tracker.code]}
        <p class="small warning">
          This sends the {sessions[tracker.code]?.trim() ? 'session cookie' : 'API key'} over plain HTTP, unencrypted.
          Save anyway only if the network between you and salmon is one you trust.
        </p>
      {/if}
      {#if saveErrors[tracker.code]}<p class="err-text small">{saveErrors[tracker.code]}</p>{/if}
      {#if job}
        {#if !FINISHED.includes(job.status)}
          <p class="muted small">Checking the connection…</p>
        {:else if job.status !== 'done'}
          <p class="err-text small">The connection check did not finish: {job.error ?? job.status}</p>
        {:else}
          {#each checksOf(job.result) as found (found.tracker)}
            <p class="small">
              {#if found.ok}<span class="chip ok">connection ok</span>{:else}<span class="chip err">connection failed</span>{/if}
            </p>
            {#each problems(found) as line (line)}<p class="muted small">{line}</p>{/each}
          {/each}
        {/if}
      {/if}
    </div>
  {/each}
{/if}

<style>
  .fields {
    display: grid;
    gap: 0.8rem;
    margin: 0.6rem 0;
  }
  .fields label {
    display: grid;
    gap: 0.3rem;
  }
  .actions {
    flex-wrap: wrap;
  }
  .small {
    font-size: 0.85rem;
  }
  .mono {
    overflow-wrap: anywhere;
  }
  .warning {
    color: var(--warn);
  }
  .err-text {
    color: var(--err);
    overflow-wrap: anywhere;
  }
</style>
