<script lang="ts">
  import { apiPost } from '../lib/api'
  import { copyText } from '../lib/clipboard'
  import ImagePicker from '../lib/ImagePicker.svelte'
  import JobActivity from '../lib/JobActivity.svelte'
  import JobStatus from '../lib/JobStatus.svelte'
  import { FINISHED, jobStore, type Job } from '../lib/jobs.svelte'
  import { loadToolsOptions, type DescgenResult, type ImageUploadResult, type ToolsOptions } from '../lib/tools'

  let options = $state<ToolsOptions | null>(null)
  let optionsError = $state('')

  // Description generator
  let descUrls = $state('')
  let descJobId = $state<string | null>(null)
  let descError = $state('')
  let descStarting = $state(false)

  // Image upload
  let picked = $state<string[]>([])
  let imageHost = $state('')
  let imageTracker = $state('')
  let imageJobId = $state<string | null>(null)
  let imageError = $state('')
  let imageStarting = $state(false)

  // What the last copy button did, shown beside it.
  let copied = $state('')

  $effect(() => {
    loadToolsOptions()
      .then((found) => {
        options = found
      })
      .catch((e) => {
        optionsError = `Could not load the options: ${e}`
      })
  })

  const descJob = $derived(descJobId ? jobStore.get(descJobId) : undefined)
  const imageJob = $derived(imageJobId ? jobStore.get(imageJobId) : undefined)
  const urls = $derived(descUrls.split(/\s+/).filter(Boolean))

  // The host the images go to, and the trackers whose pages show it: the rule is the server's, this only warns.
  const tracker = $derived(options?.trackers.find((t) => t.code === imageTracker))
  const effectiveHost = $derived(imageHost || tracker?.image_host || options?.image_host || '')
  const onlyFor = $derived(options?.image_hosts.find((h) => h.name === effectiveHost)?.only_for ?? [])
  const hostProblem = $derived(
    onlyFor.length === 0
      ? ''
      : !imageTracker
        ? `${effectiveHost}'s images only display on ${onlyFor.join('/')}: name the tracker they are for.`
        : !onlyFor.includes(imageTracker)
          ? `${effectiveHost}'s images only display on ${onlyFor.join('/')}, not on ${imageTracker}.`
          : '',
  )

  async function start(kind: string, params: Record<string, unknown>): Promise<Job> {
    const job = await apiPost<Job>('/jobs', { kind, params })
    void jobStore.show(job.id)
    return job
  }

  async function generate() {
    descError = ''
    descStarting = true
    try {
      descJobId = (await start('descgen', { urls })).id
    } catch (e) {
      descError = String(e)
    } finally {
      descStarting = false
    }
  }

  async function upload() {
    imageError = ''
    imageStarting = true
    try {
      imageJobId = (
        await start('images', { paths: picked, host: imageHost || null, tracker: imageTracker || null })
      ).id
    } catch (e) {
      imageError = String(e)
    } finally {
      imageStarting = false
    }
  }

  async function copy(label: string, text: string) {
    copied = (await copyText(text)) ? `${label} copied` : `Could not copy ${label}: select it and copy by hand`
  }
</script>

<!-- Ported from the fork's Tools page (chodeus 0b29d2d5, 9bfdddc3, 1f8e47d8, 18fc08d9), trimmed to the description
     generator and the image upload, each a job of its own. The copy buttons fill this browser's clipboard. -->
<h1>Tools</h1>
<p class="lead">
  Build a tracklist description from store URLs, as <span class="mono">salmon descgen</span> does, and upload images,
  as <span class="mono">salmon images up</span> does.
</p>

{#if optionsError}<div class="card"><p class="error">{optionsError}</p></div>{/if}

<div class="card">
  <h2>Description generator</h2>
  <p class="muted">
    Release URLs from the metadata sources (one per line, at most {options?.max_descgen_urls ?? 10}). Each is
    fetched once from the store.
  </p>
  <textarea class="mono" rows="4" bind:value={descUrls} placeholder="https://…"></textarea>
  <div class="row actions">
    <button class="btn" onclick={generate} disabled={!urls.length || descStarting}>Generate</button>
  </div>
  {#if descError}<p class="error">{descError}</p>{/if}

  {#if descJob}
    <JobStatus job={descJob} />
    {#if descJob.status === 'done'}
      <pre class="result mono">{(descJob.result as DescgenResult).description}</pre>
      <div class="row">
        <button
          class="btn small"
          onclick={() => copy('Description', (descJob.result as DescgenResult).description)}
        >
          Copy description
        </button>
        <span class="muted">{copied}</span>
      </div>
    {:else if !FINISHED.includes(descJob.status)}
      <JobActivity job={descJob} logTail={10} />
    {/if}
  {/if}
</div>

<div class="card">
  <h2>Image upload</h2>
  <p class="muted">
    Pick images in the folders salmon may read. Only JPEG, PNG, GIF and WebP files are sent (judged by what is in
    them, not by their names), at most {options?.max_images ?? 20} at a time, never one from a folder of
    <span class="mono">.torrent</span> files.
  </p>
  <ImagePicker bind:selected={picked} max={options?.max_images ?? 20} />
  <div class="row options">
    <label>
      Host
      <select bind:value={imageHost}>
        <option value="">
          Default{tracker ? ` for ${tracker.code}` : ''} ({tracker?.image_host ?? options?.image_host ?? '…'})
        </option>
        {#each options?.image_hosts ?? [] as host (host.name)}
          <option value={host.name}>{host.name}{host.only_for.length ? ` (${host.only_for.join('/')} only)` : ''}</option>
        {/each}
      </select>
    </label>
    <label>
      For tracker
      <select bind:value={imageTracker}>
        <option value="">None</option>
        {#each options?.trackers ?? [] as t (t.code)}<option value={t.code}>{t.code}</option>{/each}
      </select>
    </label>
  </div>
  {#if hostProblem}<p class="error">{hostProblem}</p>{/if}
  <div class="row actions">
    <button class="btn" onclick={upload} disabled={!picked.length || imageStarting || hostProblem !== ''}>
      Upload {picked.length || ''} image{picked.length === 1 ? '' : 's'}
    </button>
  </div>
  {#if imageError}<p class="error">{imageError}</p>{/if}

  {#if imageJob}
    <JobStatus job={imageJob} />
    {#if imageJob.status === 'done'}
      {@const result = imageJob.result as ImageUploadResult}
      <p class="muted">Uploaded to {result.host}.</p>
      <ul class="urls">
        {#each result.images as image (image.url + image.file)}
          <li>
            <span class="mono grow">{image.url}</span>
            <button class="btn small secondary" onclick={() => copy(image.file, image.url)}>Copy</button>
          </li>
        {/each}
      </ul>
      <div class="row">
        <button
          class="btn small"
          onclick={() => copy('The URLs', result.images.map((image) => image.url).join('\n'))}
        >
          Copy all
        </button>
        <span class="muted">{copied}</span>
      </div>
    {:else if !FINISHED.includes(imageJob.status)}
      <JobActivity job={imageJob} logTail={10} />
    {/if}
  {/if}
</div>

<style>
  textarea {
    width: 100%;
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.5rem 0.7rem;
    font-size: 0.9rem;
    resize: vertical;
  }
  textarea:focus {
    outline: none;
    border-color: var(--accent-dim);
  }
  .actions {
    margin-top: 0.7rem;
  }
  .options {
    margin-top: 0.7rem;
    flex-wrap: wrap;
    align-items: flex-end;
  }
  .options label {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    font-size: 0.85rem;
    color: var(--text-dim);
  }
  .error {
    color: var(--err);
    margin: 0.5rem 0 0;
  }
  .result {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.7rem;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: 420px;
    overflow-y: auto;
    margin: 0.8rem 0;
  }
  .urls {
    list-style: none;
    padding: 0;
    margin: 0 0 0.6rem;
  }
  .urls li {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.15rem 0;
  }
  .urls .mono {
    overflow-wrap: anywhere;
  }
</style>
