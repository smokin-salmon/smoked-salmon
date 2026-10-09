<script lang="ts">
  import { apiGet } from '../lib/api'
  import { FINISHED, jobStore } from '../lib/jobs.svelte'

  interface Space {
    free_bytes: number | null
    total_bytes: number | null
  }
  interface Overview {
    version: string | null
    trackers: string[]
    roots: ({ path: string; name: string; library: boolean } & Space)[]
    tmp: ({ name: string } & Space) | null
    tools: { required: Record<string, boolean>; optional: Record<string, boolean> }
    jobs: Record<string, number>
    max_jobs: number
  }

  let overview = $state<Overview | null>(null)
  let error = $state('')

  // Read once: nothing here asks a tracker anything.
  $effect(() => {
    apiGet<Overview>('/dashboard')
      .then((answer) => (overview = answer))
      .catch((e) => (error = String(e)))
  })

  const missing = $derived(overview ? Object.values(overview.tools.required).filter((found) => !found).length : 0)

  function usedPercent(space: Space): number | null {
    if (space.free_bytes === null || !space.total_bytes) return null
    const percent = Math.round(((space.total_bytes - space.free_bytes) / space.total_bytes) * 100)
    return Math.min(100, Math.max(0, percent))
  }

  function formatBytes(bytes: number): string {
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB']
    let value = bytes
    let unit = 0
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024
      unit += 1
    }
    return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`
  }

  // Live, from the job events.
  const running = $derived(jobStore.jobs.filter((j) => j.status === 'running').length)
  const waiting = $derived(jobStore.jobs.filter((j) => j.status === 'waiting').length)
  const queued = $derived(jobStore.jobs.filter((j) => j.status === 'queued').length)
  const finished = $derived(jobStore.jobs.filter((j) => FINISHED.includes(j.status)).length)
</script>

<!-- Ported from the fork's Dashboard (chodeus, 9bfdddc3, 0b29d2d5), without the connection check: no tracker request
     on load. -->

{#snippet usage(space: Space)}
  {@const used = usedPercent(space)}
  {#if used === null}
    <span class="chip">usage unavailable</span>
  {:else}
    <div class="disk" title="{formatBytes(space.free_bytes ?? 0)} free of {formatBytes(space.total_bytes ?? 0)}">
      <div class="disk-bar">
        <div class="disk-fill {used >= 90 ? 'err' : used >= 75 ? 'warn' : ''}" style="width: {used}%"></div>
      </div>
      <span class="muted">{formatBytes(space.free_bytes ?? 0)} free</span>
    </div>
  {/if}
{/snippet}

<h1>Dashboard</h1>
<p class="lead">What this salmon is set up with, and what it is doing.</p>

{#if error}
  <div class="card"><p class="muted">Could not load the overview: {error}</p></div>
{:else if !overview}
  <p class="muted">Loading…</p>
{:else}
  <div class="tiles">
    <div class="tile {running ? 'run' : ''}">
      <span class="figure">{running}<span class="of">/{overview.max_jobs}</span></span>
      <span class="muted">jobs running</span>
    </div>
    <a class="tile {waiting ? 'warn' : ''}" href="#/jobs">
      <span class="figure">{waiting}</span>
      <span class="muted">waiting for an answer</span>
    </a>
    <div class="tile">
      <span class="figure">{queued}</span>
      <span class="muted">waiting their turn</span>
    </div>
    <div class="tile">
      <span class="figure">{finished}</span>
      <span class="muted">finished</span>
    </div>
  </div>

  <div class="card">
    <h2>smoked-salmon {overview.version ?? ''}</h2>
    <p class="row wrap">
      Trackers:
      {#each overview.trackers as tracker (tracker)}
        <span class="chip ok">{tracker}</span>
      {:else}
        <span class="chip err">none configured</span>
      {/each}
    </p>
    <p class="muted small">The connections to the trackers are not checked here: run <span class="mono">salmon checkconf</span>.</p>
  </div>

  <div class="card">
    <h2>Folders</h2>
    <p class="muted small">The folders the browser lists and jobs may work in. Library albums are never changed.</p>
    <table>
      <tbody>
        {#each overview.roots as root (root.path)}
          <tr>
            <td>{root.library ? 'library_dirs' : 'download_directory'}</td>
            <td class="mono">{root.path}</td>
            <td>{@render usage(root)}</td>
          </tr>
        {/each}
        {#if overview.tmp}
          <tr>
            <td>tmp_dir</td>
            <td class="mono">{overview.tmp.name}</td>
            <td>{@render usage(overview.tmp)}</td>
          </tr>
        {/if}
      </tbody>
    </table>
  </div>

  <div class="card">
    <h2>Tools</h2>
    <p class="muted small">
      {missing ? `${missing} required tool${missing === 1 ? '' : 's'} not found on PATH.` : 'All required tools are on PATH.'}
      The same check as <span class="mono">salmon health</span>.
    </p>
    <table>
      <tbody>
        {#each Object.entries(overview.tools.required) as [name, found] (name)}
          <tr>
            <td class="mono">{name}</td>
            <td><span class="chip {found ? 'ok' : 'err'}">{found ? 'found' : 'missing'}</span></td>
          </tr>
        {/each}
        {#each Object.entries(overview.tools.optional) as [name, found] (name)}
          <tr>
            <td class="mono">{name} <span class="muted">(optional)</span></td>
            <td><span class="chip {found ? 'ok' : ''}">{found ? 'found' : 'missing'}</span></td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
{/if}

<style>
  .tiles {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(160px, 100%), 1fr));
    gap: 0.7rem;
    margin-bottom: 0.9rem;
  }
  .tile {
    background: var(--bg-raised);
    border: 1px solid var(--border);
    border-left: 3px solid var(--border);
    border-radius: 10px;
    padding: 0.7rem 0.8rem;
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
    color: var(--text);
  }
  a.tile:hover {
    text-decoration: none;
    background: var(--bg-hover);
  }
  .tile.run {
    border-left-color: var(--accent);
  }
  .tile.warn {
    border-left-color: var(--warn);
  }
  .figure {
    font-size: 1.5rem;
    font-weight: 700;
    line-height: 1.1;
  }
  .figure .of {
    font-size: 0.9rem;
    font-weight: 500;
    color: var(--text-dim);
  }
  .wrap {
    flex-wrap: wrap;
    gap: 0.4rem;
  }
  .small {
    font-size: 0.85rem;
  }
  td.mono {
    overflow-wrap: anywhere;
  }
  .disk {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    min-width: 160px;
  }
  .disk-bar {
    flex: 1;
    height: 6px;
    border-radius: 3px;
    background: var(--border);
    overflow: hidden;
  }
  .disk-fill {
    height: 100%;
    background: var(--accent);
  }
  .disk-fill.warn {
    background: var(--warn);
  }
  .disk-fill.err {
    background: var(--err);
  }
</style>
