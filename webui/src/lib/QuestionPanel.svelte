<script lang="ts">
  import { tick } from 'svelte'
  import { apiPost } from './api'
  import type { Job, Picked } from './jobs.svelte'
  import SpectralViewer from './SpectralViewer.svelte'
  import TypedQuestion from './TypedQuestion.svelte'

  let { job }: { job: Job } = $props()

  let text = $state('')
  let editText = $state('')
  let sendError = $state('')
  let sending = $state(false)
  let lastQuestionId = ''

  const VIEW_KEY = 'salmon.spectralsView'
  type View = 'single' | 'grid'

  /** The viewer's last choice; blocked or cleared storage (a private window) falls back to one at a time. */
  function rememberedView(): View {
    try {
      return localStorage.getItem(VIEW_KEY) === 'grid' ? 'grid' : 'single'
    } catch {
      return 'single'
    }
  }

  let view = $state<View>(rememberedView())
  let index = $state(0)
  let viewer = $state<HTMLElement>()

  function setView(next: View) {
    if (next === view) return
    view = next
    try {
      localStorage.setItem(VIEW_KEY, next)
    } catch {
      // Not remembered: the choice still holds until the page is closed.
    }
  }

  function openSingle(at: number) {
    index = at
    setView('single')
    void tick().then(() => viewer?.focus())
  }

  const spectrals = $derived(
    job.question?.kind === 'spectrals'
      ? (job.question.files ?? []).map((file) => {
          const name = job.question?.tracks?.[file.slice(0, 2)]
          return { url: spectralUrl(file), label: name ? `${file}: ${name}` : file }
        })
      : [],
  )

  // Stay in range if the files change.
  $effect(() => {
    if (index > 0 && index >= spectrals.length) index = Math.max(0, spectrals.length - 1)
  })

  $effect(() => {
    const q = job.question
    if (q && q.id !== lastQuestionId) {
      lastQuestionId = q.id
      index = 0
      if (q.kind === 'spectrals') {
        // Keys work without a click, unless something else has the focus.
        void tick().then(() => {
          const active = document.activeElement
          if (viewer && (active === null || active === document.body)) viewer.focus()
        })
      }
      text = typeof q.default === 'string' ? q.default : ''
      editText = q.initial ?? ''
      sendError = ''
    }
  })

  function spectralUrl(file: string): string {
    return `/api/jobs/${encodeURIComponent(job.id)}/spectrals/${encodeURIComponent(file)}`
  }

  async function send(value: string | boolean | null | Picked) {
    if (!job.question || sending) return
    sending = true
    sendError = ''
    try {
      await apiPost(`/jobs/${encodeURIComponent(job.id)}/answer`, { question_id: job.question.id, value })
    } catch (e) {
      sendError = String(e)
    } finally {
      sending = false
    }
  }
</script>

<!-- Ported from the fork's QuestionPanel (styx-techno, chodeus), with the spectrals and errors added. -->
{#if job.question}
  {@const q = job.question}
  <div class="question card">
    <pre class="qtext">{q.text}</pre>

    {#if q.kind === 'confirm'}
      <div class="row">
        <button class="btn" disabled={sending} onclick={() => send(true)}>
          {q.labels?.[0] ?? 'Yes'}{q.default === true ? ' (default)' : ''}
        </button>
        <button class="btn secondary" disabled={sending} onclick={() => send(false)}>
          {q.labels?.[1] ?? 'No'}{q.default === false ? ' (default)' : ''}
        </button>
      </div>
    {:else if q.kind === 'edit'}
      <textarea bind:value={editText} rows={Math.min(24, Math.max(6, editText.split('\n').length + 1))}></textarea>
      <div class="row actions">
        <button class="btn" disabled={sending} onclick={() => send(editText)}>Save</button>
        <button class="btn secondary" disabled={sending} onclick={() => send(null)}>Leave unchanged</button>
      </div>
    {:else if q.kind === 'spectrals'}
      <div class="row views">
        <button class="btn small secondary" onclick={() => {
            if (view === 'single') setView('grid')
            else openSingle(index)
          }}>
          {view === 'single' ? 'Grid' : 'One at a time'}
        </button>
      </div>
      {#if view === 'single'}
        <SpectralViewer images={spectrals} bind:index bind:root={viewer} onenter={() => send(true)} />
        <p class="muted hint centered">← → to step, Enter for Done</p>
      {:else}
        <div class="gallery">
          {#each spectrals as image, i (image.url)}
            <a
              href={image.url}
              onclick={(e) => {
                e.preventDefault()
                openSingle(i)
              }}
            >
              <img src={image.url} alt={image.label} loading="lazy" />
              <span class="muted mono">{image.label}</span>
            </a>
          {/each}
        </div>
      {/if}
      <div class="row actions done">
        <button class="btn" disabled={sending} onclick={() => send(true)}>Done</button>
      </div>
    {:else if q.options}
      <TypedQuestion {q} {sending} {send} />
    {:else}
      {#if q.choices}
        <div class="row wrap">
          {#each q.choices as choice (choice)}
            <button class="btn secondary" disabled={sending} onclick={() => send(choice)}>{choice}</button>
          {/each}
        </div>
      {/if}
      <form
        class="row actions"
        onsubmit={(e) => {
          e.preventDefault()
          send(text)
        }}
      >
        <input type="text" class="grow mono" bind:value={text} placeholder="Answer" />
        <button class="btn" disabled={sending}>Send</button>
      </form>
      {#if typeof q.default === 'string'}
        <p class="muted hint">Empty for the default: <span class="mono">{q.default}</span></p>
      {/if}
    {/if}

    {#if q.error}<p class="error">{q.error}</p>{/if}
    {#if sendError}<p class="error">{sendError}</p>{/if}
  </div>
{/if}

<style>
  .question {
    border-color: var(--accent-dim);
    background: #221a1c;
    margin: 0.8rem 0 0;
  }
  .qtext {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font-family: inherit;
    margin: 0 0 0.7rem;
  }
  .wrap {
    flex-wrap: wrap;
    margin-bottom: 0.5rem;
  }
  .actions {
    margin-top: 0.5rem;
  }
  .done {
    position: sticky;
    bottom: 0;
    background: #221a1c;
    padding: 0.5rem 0;
    z-index: 1;
  }
  .views {
    margin-bottom: 0.6rem;
  }
  .centered {
    text-align: center;
  }
  .hint {
    font-size: 0.8rem;
    margin: 0.4rem 0 0;
  }
  .error {
    color: var(--err);
    margin: 0.5rem 0 0;
  }
  textarea {
    width: 100%;
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.5rem 0.7rem;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.85rem;
  }
  .gallery {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(min(280px, 100%), 1fr));
    gap: 0.7rem;
  }
  .gallery img {
    width: 100%;
    border: 1px solid var(--border);
    border-radius: 6px;
  }
  .gallery span {
    font-size: 0.72rem;
    display: block;
    text-align: center;
    overflow-wrap: anywhere;
  }
  @media (max-width: 700px) {
    textarea {
      font-size: 16px;
    }
  }
</style>
