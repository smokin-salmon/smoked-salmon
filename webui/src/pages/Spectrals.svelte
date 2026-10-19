<script lang="ts">
  import { tick } from 'svelte'
  import { apiPost } from '../lib/api'
  import { copyText } from '../lib/clipboard'
  import FolderPicker from '../lib/FolderPicker.svelte'
  import JobActivity from '../lib/JobActivity.svelte'
  import JobStatus from '../lib/JobStatus.svelte'
  import QuestionPanel from '../lib/QuestionPanel.svelte'
  import SpectralViewer from '../lib/SpectralViewer.svelte'
  import { FINISHED, jobStore, type Job } from '../lib/jobs.svelte'
  import { loadToolsOptions, type SpectralsUploadResult, type ToolsOptions } from '../lib/tools'

  let path = $state('')
  let error = $state('')
  let starting = $state(false)
  let lightbox = $state<{ job: Job; index: number } | null>(null)
  let lightboxViewer = $state<HTMLElement>()
  let lightboxDialog = $state<HTMLDialogElement>()
  let opener: HTMLElement | null = null
  let discarding = $state<string[]>([])

  // The upload of a job's spectrals: the tracker picked for each job, and the upload job started for it.
  let options = $state<ToolsOptions | null>(null)
  let trackerFor = $state<Record<string, string>>({})
  let uploadFor = $state<Record<string, string>>({})
  let uploading = $state<string[]>([])
  let copied = $state('')

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

  $effect(() => {
    loadToolsOptions()
      .then((found) => {
        options = found
      })
      .catch((e) => {
        error = `Could not load the options: ${e}`
      })
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

  /** The spectrals host the tracker picked for a job maps to, as the config says (none: the shared setting). */
  function hostFor(job: Job): string {
    const code = trackerFor[job.id] ?? ''
    return options?.trackers.find((t) => t.code === code)?.spectrals_host ?? options?.spectrals_host ?? '…'
  }

  async function uploadSpectrals(job: Job) {
    error = ''
    uploading = [...uploading, job.id]
    try {
      const started = await apiPost<Job>('/jobs', {
        kind: 'spectrals_upload',
        params: { job_id: job.id, tracker: trackerFor[job.id] || null },
      })
      void jobStore.show(started.id)
      uploadFor = { ...uploadFor, [job.id]: started.id }
    } catch (e) {
      error = String(e)
    } finally {
      uploading = uploading.filter((id) => id !== job.id)
    }
  }

  async function copy(label: string, text: string) {
    copied = (await copyText(text)) ? `${label} copied` : `Could not copy ${label}: select it and copy by hand`
  }

  function imageUrl(job: Job, file: string): string {
    return `/api/jobs/${encodeURIComponent(job.id)}/spectrals/${encodeURIComponent(file)}`
  }

  function openLightbox(job: Job, at: number, from: HTMLElement) {
    opener = from
    lightbox = { job, index: at }
    void tick().then(() => lightboxViewer?.focus())
  }

  /** A native modal dialog: focus stays inside it, the page behind is inert, and Escape fires `close` too. */
  function modal(node: HTMLDialogElement) {
    node.showModal()
  }

  function closeLightbox() {
    lightboxDialog?.close()
  }

  function closed() {
    lightbox = null
    opener?.focus()
    opener = null
  }

  const lightboxImages = $derived(
    lightbox
      ? (lightbox.job.spectrals ?? []).map((file) => {
          const name = track(lightbox!.job, file)
          return { url: imageUrl(lightbox!.job, file), label: name ? `${file}: ${name}` : file }
        })
      : [],
  )

  function track(job: Job, file: string): string {
    const tracks = (job.result as { tracks?: Record<string, string> } | null)?.tracks
    return tracks?.[file.slice(0, 2)] ?? ''
  }
</script>

<!-- Ported from the fork's Spectrals page (chodeus, d6ac6372, 0b29d2d5, 9bfdddc3, 1f8e47d8, 18fc08d9): the upload is a
     job of its own, from the images of the spectrals job only. -->
<h1>Spectrals</h1>
<p class="lead">
  The spectrals of an album and what the frequency analysis measures, as <span class="mono">salmon specs</span> shows
  them. They are written to a folder of salmon's own, never next to the music, and nothing is uploaded unless you
  choose to upload them to an image host. Discard them when you are done; salmon also removes them when it stops.
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
        {#each job.spectrals as file, i (file)}
          <figure>
            <button onclick={(e) => openLightbox(job, i, e.currentTarget)}>
              <img src={imageUrl(job, file)} alt={file} loading="lazy" />
            </button>
            <figcaption class="muted mono">{file}{#if track(job, file)}: {track(job, file)}{/if}</figcaption>
          </figure>
        {/each}
      </div>
    {/if}

    {#if job.status === 'done' && job.spectrals?.length}
      {@const upload = uploadFor[job.id] ? jobStore.get(uploadFor[job.id]) : undefined}
      <div class="upload">
        <h3>Upload spectrals</h3>
        <p class="muted">
          Sends the images of each track (not the frequency plots) to the spectrals host of the tracker you pick, as
          <span class="mono">salmon specs</span> does.
        </p>
        <div class="row wrap">
          <label>
            For tracker
            <select bind:value={trackerFor[job.id]}>
              <option value="">None (the shared setting)</option>
              {#each options?.trackers ?? [] as t (t.code)}<option value={t.code}>{t.code}</option>{/each}
            </select>
          </label>
          <span class="muted">Host: {hostFor(job)}</span>
          <button
            class="btn small"
            disabled={uploading.includes(job.id) || (upload !== undefined && !FINISHED.includes(upload.status))}
            onclick={() => uploadSpectrals(job)}
          >
            Upload spectrals
          </button>
        </div>
        {#if upload}
          <JobStatus job={upload} />
          <QuestionPanel job={upload} />
          {#if upload.status === 'done'}
            {@const result = upload.result as SpectralsUploadResult}
            <p class="muted">Uploaded to {result.host}.</p>
            <pre class="result mono">{result.text}</pre>
            <div class="row wrap">
              <button class="btn small" onclick={() => copy('The URLs', result.text)}>Copy</button>
              <button class="btn small secondary" onclick={() => copy('The BBCode', result.bbcode)}>Copy as BBCode</button>
              <span class="muted">{copied}</span>
            </div>
          {:else if !FINISHED.includes(upload.status)}
            <JobActivity job={upload} logTail={10} />
          {/if}
        {/if}
      </div>
    {/if}
  </div>
{:else}
  <div class="card"><p class="muted">No spectrals kept.</p></div>
{/each}

{#if lightbox}
  <dialog
    class="lightbox"
    aria-label="Spectral"
    use:modal
    bind:this={lightboxDialog}
    onclose={closed}
    onclick={(e) => {
      if (e.target === e.currentTarget) closeLightbox()
    }}
  >
    <SpectralViewer images={lightboxImages} bind:index={lightbox.index} bind:root={lightboxViewer} />
    <button class="btn small secondary close" onclick={closeLightbox}>Close</button>
  </dialog>
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
  .upload {
    margin-top: 1rem;
    padding-top: 0.8rem;
    border-top: 1px solid var(--border);
  }
  .upload .wrap {
    flex-wrap: wrap;
  }
  .upload label {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    font-size: 0.85rem;
    color: var(--text-dim);
  }
  .result {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.7rem;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: 320px;
    overflow-y: auto;
    margin: 0.8rem 0;
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
    width: 100vw;
    height: 100vh;
    max-width: none;
    max-height: none;
    margin: 0;
    border: none;
    background: transparent;
    color: inherit;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 0.8rem;
    padding: 2rem;
  }
  .lightbox[open] {
    display: flex;
  }
  .lightbox::backdrop {
    background: rgba(0, 0, 0, 0.85);
  }
  .close {
    position: absolute;
    top: 0.8rem;
    right: 0.8rem;
  }
</style>
