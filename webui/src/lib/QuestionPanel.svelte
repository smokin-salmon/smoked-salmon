<script lang="ts">
  import { apiPost } from './api'
  import type { Job } from './jobs.svelte'

  let { job }: { job: Job } = $props()

  let text = $state('')
  let editText = $state('')
  let sendError = $state('')
  let sending = $state(false)
  let lastQuestionId = ''

  $effect(() => {
    const q = job.question
    if (q && q.id !== lastQuestionId) {
      lastQuestionId = q.id
      text = typeof q.default === 'string' ? q.default : ''
      editText = q.initial ?? ''
      sendError = ''
    }
  })

  function spectralUrl(file: string): string {
    return `/api/jobs/${encodeURIComponent(job.id)}/spectrals/${encodeURIComponent(file)}`
  }

  async function send(value: string | boolean | null) {
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
          Yes{q.default === true ? ' (default)' : ''}
        </button>
        <button class="btn secondary" disabled={sending} onclick={() => send(false)}>
          No{q.default === false ? ' (default)' : ''}
        </button>
      </div>
    {:else if q.kind === 'edit'}
      <textarea bind:value={editText} rows={Math.min(24, Math.max(6, editText.split('\n').length + 1))}></textarea>
      <div class="row actions">
        <button class="btn" disabled={sending} onclick={() => send(editText)}>Save</button>
        <button class="btn secondary" disabled={sending} onclick={() => send(null)}>Leave unchanged</button>
      </div>
    {:else if q.kind === 'spectrals'}
      <div class="gallery">
        {#each q.files ?? [] as file (file)}
          <a href={spectralUrl(file)} target="_blank" rel="noreferrer">
            <img src={spectralUrl(file)} alt={file} loading="lazy" />
            <span class="muted mono">
              {file}{#if q.tracks?.[file.slice(0, 2)]}: {q.tracks[file.slice(0, 2)]}{/if}
            </span>
          </a>
        {/each}
      </div>
      <div class="row actions">
        <button class="btn" disabled={sending} onclick={() => send(true)}>Done</button>
      </div>
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
