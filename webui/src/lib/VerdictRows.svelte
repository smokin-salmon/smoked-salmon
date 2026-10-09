<script lang="ts">
  import { CHIP, type Row } from './verdicts'

  let { rows }: { rows: Row[] } = $props()
</script>

<!-- Ported from the fork's VerdictRows (chodeus, 9bfdddc3), without the acknowledgements the upload page needs. -->
<ul class="rows">
  {#each rows as row, index (index)}
    <li class="verdict-{row.verdict.toLowerCase()}">
      <span class="chip {CHIP[row.verdict]}">{row.verdict}</span>
      <span class="label">{row.check}</span>
      <span class="detail">{row.detail}</span>
      {#if row.notes.length}
        <ul class="notes">
          {#each row.notes as note, n (n)}<li>{note}</li>{/each}
        </ul>
      {/if}
    </li>
  {/each}
</ul>

<style>
  .rows {
    list-style: none;
    margin: 0.6rem 0 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
  }
  .rows > li {
    display: grid;
    grid-template-columns: 4.2rem 11rem 1fr;
    align-items: baseline;
    gap: 0.5rem;
    font-size: 0.85rem;
  }
  .chip {
    justify-self: start;
  }
  .label {
    font-weight: 600;
  }
  .detail {
    color: var(--text-dim);
    white-space: pre-line;
    overflow-wrap: anywhere;
  }
  .verdict-info .label,
  .verdict-info .detail {
    opacity: 0.6;
  }
  .notes {
    grid-column: 3;
    margin: 0;
    padding-left: 1rem;
    color: var(--text-dim);
    font-size: 0.8rem;
    overflow-wrap: anywhere;
  }
  @media (max-width: 640px) {
    .rows > li {
      grid-template-columns: auto 1fr;
    }
    .detail,
    .notes {
      grid-column: 1 / -1;
    }
  }
</style>
