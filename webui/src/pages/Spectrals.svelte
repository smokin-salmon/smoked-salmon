<script lang="ts">
  import { t } from '../lib/i18n'
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
  // Whether the picker has checked the folder and a job may start on it.
  let usable = $state(false)
  let error = $state('')
  let starting = $state(false)
  let lightbox = $state<{ job: Job; index: number } | null>(null)
  let lightboxViewer = $state<HTMLElement>()
  let lightboxDialog = $state<HTMLDialogElement>()
  let opener: HTMLElement | null = null
  let pressedOnBackdrop = false
  let discarding = $state<string[]>([])

  // The upload of a job's spectrals: the tracker picked for each job, and the upload job started for it.
  let options = $state<ToolsOptions | null>(null)
  let trackerFor = $state<Record<string, string>>({})
  let uploadFor = $state<Record<string, string>>({})
  let uploading = $state<string[]>([])
  let copied = $state('')
  // The tracks ticked for each job's upload: until the user changes it, the job's own default selection.
  let pickedFor = $state<Record<string, number[]>>({})

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
        error = `无法加载选项： ${e}`
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

  /** The tracks of a job, by number, as its result lists them. */
  function trackList(job: Job): { id: number; name: string }[] {
    const tracks = (job.result as { tracks?: Record<string, string> } | null)?.tracks ?? {}
    return Object.entries(tracks)
      .map(([id, name]) => ({ id: Number(id), name }))
      .sort((a, b) => a.id - b.id)
  }

  /** The tracks `salmon specs` would upload by default, worked out by the server when the job finished. */
  function picked(job: Job): number[] {
    return pickedFor[job.id] ?? (job.result as { selection?: number[] } | null)?.selection ?? []
  }

  function toggle(job: Job, id: number, on: boolean) {
    const rest = picked(job).filter((n) => n !== id)
    pickedFor = { ...pickedFor, [job.id]: on ? [...rest, id].sort((a, b) => a - b) : rest }
  }

  async function uploadSpectrals(job: Job) {
    error = ''
    uploading = [...uploading, job.id]
    try {
      const started = await apiPost<Job>('/jobs', {
        kind: 'spectrals_upload',
        params: { job_id: job.id, tracker: trackerFor[job.id] || null, spectral_ids: picked(job) },
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
    copied = (await copyText(text)) ? `${label} 复制` : `无法复制 ${label}:选择并手动复制`
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
<h1>频谱</h1>
<p class="lead">
  生成专辑频谱并查看频率分析结果，对应 <span class="mono">salmon specs</span>。频谱保存在程序的临时目录中；选择上传后才会发送到图床。查看完毕可手动清理，程序停止时也会自动删除。
</p>

<div class="card">
  <FolderPicker bind:value={path} bind:usable />
  <div class="row actions">
    <button class="btn" onclick={start} disabled={!usable || starting}>生成频谱</button>
  </div>
  {#if error}<p class="error">{error}</p>{/if}
</div>

{#each shown as job (job.id)}
  <div class="card">
    <div class="row head">
      <h2 class="grow">{t(job.title)}</h2>
      {#if FINISHED.includes(job.status) && job.spectrals !== null}
        <button class="btn small secondary" disabled={discarding.includes(job.id)} onclick={() => discard(job)}>
          清除这些频谱
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
        <h3>上传频谱</h3>
        <p class="muted">
          将您勾选的轨迹的图像（不是频率图）发送到您选择的站点的频谱主机，如 <span class="mono">salmon specs</span> 确实如此。勾选项从它开始的地方开始：你的
          <span class="mono">default_spectral_ids</span>，否则是随机的三分之一曲目。
        </p>
        <fieldset class="tracks">
          <legend>选择要上传频谱的曲目</legend>
          <div class="row wrap shortcuts">
            <button
              class="btn small secondary"
              onclick={() => (pickedFor = { ...pickedFor, [job.id]: trackList(job).map((t) => t.id) })}
            >
              全部
            </button>
            <button class="btn small secondary" onclick={() => (pickedFor = { ...pickedFor, [job.id]: [] })}>
              无
            </button>
            <span class="muted">{picked(job).length} / {trackList(job).length}</span>
          </div>
          {#each trackList(job) as t (t.id)}
            <label class="track">
              <input
                type="checkbox"
                checked={picked(job).includes(t.id)}
                onchange={(e) => toggle(job, t.id, e.currentTarget.checked)}
              />
              <span class="mono">{String(t.id).padStart(2, '0')} {t.name}</span>
            </label>
          {/each}
        </fieldset>
        <div class="row wrap">
          <label>
            适用站点
            <select bind:value={trackerFor[job.id]}>
              <option value="">无（共享设置）</option>
              {#each options?.trackers ?? [] as t (t.code)}<option value={t.code}>{t.code}</option>{/each}
            </select>
          </label>
          <span class="muted">图床： {hostFor(job)}</span>
          <button
            class="btn small"
            disabled={uploading.includes(job.id) ||
              picked(job).length === 0 ||
              (upload !== undefined && !FINISHED.includes(upload.status))}
            onclick={() => uploadSpectrals(job)}
          >
            上传频谱
          </button>
        </div>
        {#if upload}
          <JobStatus job={upload} />
          <QuestionPanel job={upload} />
          {#if upload.status === 'done'}
            {@const result = upload.result as SpectralsUploadResult}
            <p class="muted">已上传至 {result.host}.</p>
            <pre class="result mono">{result.text}</pre>
            <div class="row wrap">
              <button class="btn small" onclick={() => copy('The URLs', result.text)}>复制</button>
              <button class="btn small secondary" onclick={() => copy('The BBCode', result.bbcode)}>复制为BBCode</button>
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
  <div class="card"><p class="muted">暂无保留的频谱图。</p></div>
{/each}

{#if lightbox}
  <dialog
    class="lightbox"
    aria-label="频谱"
    use:modal
    bind:this={lightboxDialog}
    onclose={closed}
    onmousedown={(e) => (pressedOnBackdrop = e.target === e.currentTarget)}
    onmouseup={(e) => {
      // Close only when the press and the release are both on the backdrop.
      if (pressedOnBackdrop && e.target === e.currentTarget) closeLightbox()
      pressedOnBackdrop = false
    }}
  >
    <SpectralViewer images={lightboxImages} bind:index={lightbox.index} bind:root={lightboxViewer} />
    <button class="btn small secondary close" onclick={closeLightbox}>关闭</button>
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
  .tracks {
    border: 1px solid var(--border);
    border-radius: 8px;
    margin: 0 0 0.8rem;
    padding: 0.5rem 0.7rem;
    min-width: 0;
  }
  .tracks legend {
    font-size: 0.85rem;
    color: var(--text-dim);
    padding: 0 0.3rem;
  }
  .shortcuts {
    margin-bottom: 0.4rem;
  }
  .upload .tracks label.track {
    flex-direction: row;
    align-items: baseline;
    gap: 0.5rem;
    color: var(--text);
    font-size: 0.85rem;
    overflow-wrap: anywhere;
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
