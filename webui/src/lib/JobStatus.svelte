<script lang="ts">
  import type { Job, JobStatus } from './jobs.svelte'

  let { job }: { job: Job } = $props()

  const chips: Record<JobStatus, { label: string; tone: string }> = {
    queued: { label: 'queued', tone: '' },
    running: { label: 'running', tone: 'run' },
    waiting: { label: 'waiting for an answer', tone: 'warn' },
    done: { label: 'done', tone: 'ok' },
    failed: { label: 'failed', tone: 'err' },
    cancelled: { label: 'cancelled', tone: 'warn' },
    unknown_outcome: { label: 'unknown outcome', tone: 'err' },
  }
</script>

<div class="row wrap">
  <span class="chip {chips[job.status].tone}">{chips[job.status].label}</span>
  {#if job.dry_run}<span class="chip">dry run</span>{/if}
  {#if job.assume_defaults}<span class="chip">-yyy</span>{/if}
  {#if job.error && job.status !== 'unknown_outcome'}
    <span class="muted">{job.error}</span>
  {/if}
</div>

{#if job.status === 'unknown_outcome'}
  <!-- No retry here, on purpose: sending the request again could upload twice. -->
  <div class="unknown" role="alert">
    <strong>The request may have reached the tracker: check the site before trying again.</strong>
    {#if job.error}<p class="muted mono">{job.error}</p>{/if}
  </div>
{/if}

<style>
  .wrap {
    flex-wrap: wrap;
  }
  .unknown {
    margin-top: 0.6rem;
    padding: 0.6rem 0.8rem;
    border: 1px solid var(--err);
    border-radius: 8px;
    background: #2a1a19;
  }
  .unknown p {
    margin: 0.3rem 0 0;
    overflow-wrap: anywhere;
  }
</style>
