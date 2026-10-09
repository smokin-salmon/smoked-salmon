<script lang="ts">
  import type { Job } from './jobs.svelte'

  let { job, logTail = 0 }: { job: Job; logTail?: number } = $props()

  let logEl = $state<HTMLElement | null>(null)

  const lines = $derived(logTail > 0 ? job.log.slice(-logTail) : job.log)

  $effect(() => {
    void lines.length
    if (logEl) logEl.scrollTop = logEl.scrollHeight
  })
</script>

<!-- Ported from the fork's JobActivity (styx-techno, chodeus). -->
{#if lines.length}
  <pre class="log" bind:this={logEl}>{#each lines as line (line.n)}<span class:err={line.err}>{line.text}
</span>{/each}</pre>
{:else}
  <p class="muted">Nothing printed yet.</p>
{/if}

<style>
  .log {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.7rem;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.78rem;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: 420px;
    overflow-y: auto;
    margin: 0.8rem 0 0;
  }
  .err {
    color: var(--err);
  }
</style>
