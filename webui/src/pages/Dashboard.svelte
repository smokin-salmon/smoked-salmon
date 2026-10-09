<script lang="ts">
  import { apiGet } from '../lib/api'
  import { FINISHED, jobStore } from '../lib/jobs.svelte'

  interface Overview {
    version: string | null
    trackers: string[]
    roots: { path: string; name: string; library: boolean }[]
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

  // Live, from the job events.
  const running = $derived(jobStore.jobs.filter((j) => j.status === 'running').length)
  const waiting = $derived(jobStore.jobs.filter((j) => j.status === 'waiting').length)
  const queued = $derived(jobStore.jobs.filter((j) => j.status === 'queued').length)
  const finished = $derived(jobStore.jobs.filter((j) => FINISHED.includes(j.status)).length)
</script>

<!-- Ported from the fork's Dashboard (chodeus, 9bfdddc3), without the connection check: no tracker request on load. -->
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
</style>
