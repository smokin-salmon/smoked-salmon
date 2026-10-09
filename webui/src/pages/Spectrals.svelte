<script lang="ts">
  import { apiPost } from '../lib/api'
  import FolderPicker from '../lib/FolderPicker.svelte'
  import JobActivity from '../lib/JobActivity.svelte'
  import JobStatus from '../lib/JobStatus.svelte'
  import QuestionPanel from '../lib/QuestionPanel.svelte'
  import { FINISHED, jobStore, type Job } from '../lib/jobs.svelte'

  let path = $state('')
  let error = $state('')
  let starting = $state(false)
  let lightbox = $state<string | null>(null)
  let discarding = $state<string[]>([])

  // The jobs still running, and the finished ones whose images are kept: newest first.
  const shown = $derived(
    jobStore.jobs.filter((j) => j.kind === 'spectrals' && (!FINISHED.includes(j.status) || j.spectrals !== null)),
  )

  // Each job's whole log, loaded once: the store keeps it up to date from then on.
  const loaded = new Set<string>()
  $effect(() => {
    for (const job of shown) {
      if (loaded.has(job.id)) continue
      loaded.add(job.id)
      void jobStore.show(job.id)
    }
  })

  async function start() {
    error = ''
    starting = true
    try {
      await apiPost<Job>('/jobs', { kind: 'spectrals', params: { path } })
    } catch (e) {
      error = String(e)
    } finally {
      starting = false
    }
  }

  async function discard(job: Job) {
    error = ''
    discarding = [...discarding, job.id]
    try {
      await apiPost(`/jobs/${encodeURIComponent(job.id)}/discard`)
    } catch (e) {
      error = String(e)
    } finally {
      discarding = discarding.filter((d) => d !== job.id)
    }
  }

  function imageUrl(job: Job, file: string): string {
    return `/api/jobs/${encodeURIComponent(job.id)}/spectrals/${encodeURIComponent(file)}`
  }

  function track(job: Job, file: string): string {
    const tracks = (job.result as { tracks?: Record<string, string> } | null)?.tracks
    return tracks?.[file.slice(0, 2)] ?? ''
  }
</script>

<!-- Ported from the fork's Spectrals page (chodeus, d6ac6372), without the upload: images stay on this server. -->
<h1>Spectrals</h1>
<p class="lead">
  The spectrals of an album and what the frequency analysis measures, as <span class="mono">salmon specs</span> shows
  them. They are written to a folder of salmon's own, never next to the music, and nothing is uploaded. Discard them
  when you are done; salmon also removes them when it stops.
</p>

<div class="card">
  <FolderPicker bind:value={path} />
  <div class="row actions">
    <button class="btn" onclick={start} disabled={!path || starting}>Make spectrals</button>
  </div>
  {#if error}<p class="error">{error}</p>{/if}
</div>

{#each shown as job (job.id)}
  <div class="card">
    <div class="row head">
      <h2 class="grow">{job.title}</h2>
      {#if FINISHED.includes(job.status) && job.spectrals !== null}
        <button class="btn small secondary" disabled={discarding.includes(job.id)} onclick={() => discard(job)}>
          Discard these spectrals
        </button>
      {/if}
    </div>
    <JobStatus {job} />
    <QuestionPanel {job} />
    <JobActivity {job} logTail={FINISHED.includes(job.status) ? 0 : 20} />

    {#if FINISHED.includes(job.status) && job.spectrals?.length}
      <div class="gallery">
        {#each job.spectrals as file (file)}
          <figure>
            <button onclick={() => (lightbox = imageUrl(job, file))}>
              <img src={imageUrl(job, file)} alt={file} loading="lazy" />
            </button>
            <figcaption class="muted mono">{file}{#if track(job, file)}: {track(job, file)}{/if}</figcaption>
          </figure>
        {/each}
      </div>
    {/if}
  </div>
{:else}
  <div class="card"><p class="muted">No spectrals kept.</p></div>
{/each}

{#if lightbox}
  <button class="lightbox" onclick={() => (lightbox = null)}>
    <img src={lightbox} alt="Spectral" />
  </button>
{/if}

<style>
  .actions {
    margin-top: 0.7rem;
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
  .gallery {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(min(300px, 100%), 1fr));
    gap: 0.8rem;
    margin-top: 1rem;
  }
  figure {
    margin: 0;
  }
  figure button {
    background: none;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0;
    cursor: zoom-in;
    width: 100%;
    overflow: hidden;
  }
  figure img {
    width: 100%;
    display: block;
  }
  figcaption {
    font-size: 0.75rem;
    margin-top: 0.2rem;
    text-align: center;
    overflow-wrap: anywhere;
  }
  .lightbox {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.85);
    border: none;
    cursor: zoom-out;
    z-index: 50;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 2rem;
  }
  .lightbox img {
    max-width: 100%;
    max-height: 100%;
  }
</style>
