<script lang="ts">
  import { t } from './i18n'
  import type { Job, JobStatus } from './jobs.svelte'

  let { job }: { job: Job } = $props()

  const chips: Record<JobStatus, { label: string; tone: string }> = {
    queued: { label: "排队中", tone: '' },
    preparing: { label: "准备中", tone: '' },
    ready: { label: "已就绪", tone: '' },
    running: { label: "运行中", tone: 'run' },
    waiting: { label: "等待回答", tone: 'warn' },
    done: { label: "已完成", tone: 'ok' },
    failed: { label: "失败", tone: 'err' },
    cancelled: { label: "已取消", tone: 'warn' },
    unknown_outcome: { label: "结果待确认", tone: 'err' },
  }
</script>

<div class="row wrap">
  <span class="chip {chips[job.status].tone}">{chips[job.status].label}</span>
  {#if job.dry_run}<span class="chip">模拟运行</span>{/if}
  {#if job.assume_defaults}<span class="chip">-yyy</span>{/if}
  {#if job.error && job.status !== 'unknown_outcome'}
    <span class="muted">{t(job.error)}</span>
  {/if}
</div>

{#if job.status === 'unknown_outcome'}
  <!-- No retry here, on purpose: sending the request again could upload twice. -->
  <div class="unknown" role="alert">
    <strong>请求可能已到达站点：在重试之前检查站点。</strong>
    {#if job.error}<p class="muted mono">{t(job.error)}</p>{/if}
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
