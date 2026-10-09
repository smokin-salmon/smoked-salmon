<script lang="ts">
  import { apiPost } from '../lib/api'
  import JobActivity from '../lib/JobActivity.svelte'
  import JobStatus from '../lib/JobStatus.svelte'
  import QuestionPanel from '../lib/QuestionPanel.svelte'
  import { FINISHED, jobStore, type Job } from '../lib/jobs.svelte'

  let cancelError = $state('')
  let cancelling = $state<string[]>([])
  let opened = $state<string[]>([])

  function toggle(id: string) {
    if (opened.includes(id)) {
      opened = opened.filter((o) => o !== id)
      jobStore.hide(id)
    } else {
      opened = [...opened, id]
      void jobStore.show(id)
    }
  }

  async function cancel(job: Job) {
    cancelError = ''
    cancelling = [...cancelling, job.id]
    try {
      await apiPost(`/jobs/${encodeURIComponent(job.id)}/cancel`)
    } catch (e) {
      // A job that has just ended: its last state is on its way.
      if (!String(e).includes('ended already')) cancelError = String(e)
      cancelling = cancelling.filter((c) => c !== job.id)
    }
  }

  function when(iso: string | null): string {
    return iso ? new Date(iso).toLocaleTimeString() : ''
  }
</script>

<!-- Ported from the fork's Jobs page (styx-techno, chodeus). -->
<h1>Jobs</h1>
<p class="lead">
  What salmon web runs, and has run since it started. A job that asks something waits here for your answer. At most a
  few run at once; the others wait their turn.
</p>

{#if jobStore.loadError}
  <div class="card"><p class="muted">{jobStore.loadError}</p></div>
{/if}
{#if cancelError}
  <div class="card"><p class="muted">Cancel failed: {cancelError}</p></div>
{/if}

{#if jobStore.jobs.length === 0 && !jobStore.loadError}
  <div class="card"><p class="muted">No jobs yet.</p></div>
{/if}

{#each jobStore.jobs as job (job.id)}
  <div class="card">
    <div class="row head">
      <h2 class="grow">{job.title}</h2>
      <span class="muted mono">{job.kind} · {when(job.started_at ?? job.created_at)}</span>
      <button class="btn small secondary" onclick={() => toggle(job.id)}>
        {opened.includes(job.id) ? 'Hide log' : 'Log'}
      </button>
      {#if !FINISHED.includes(job.status)}
        <button
          class="btn small secondary"
          disabled={cancelling.includes(job.id)}
          title="A request already sent to a tracker is answered first: the job stops after it."
          onclick={() => cancel(job)}
        >
          {cancelling.includes(job.id) ? 'Cancelling…' : 'Cancel'}
        </button>
      {/if}
    </div>
    <JobStatus {job} />
    <QuestionPanel {job} />
    {#if job.question || opened.includes(job.id)}
      <!-- What led to the question, and the whole log of a job opened. -->
      <JobActivity {job} logTail={opened.includes(job.id) ? 0 : 15} />
    {/if}
  </div>
{/each}

<style>
  .head {
    flex-wrap: wrap;
    margin-bottom: 0.4rem;
  }
  .head h2 {
    margin: 0;
    overflow-wrap: anywhere;
  }
</style>
