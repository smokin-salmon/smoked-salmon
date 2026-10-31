<script lang="ts">
  import { tick } from 'svelte'
  import type { Option, Options, Picked, Question } from './jobs.svelte'

  /**
   * A prompt that describes its answers (ADR 0006): buttons, cards or checkboxes, the actions apart, and a field for
   * a URL or an ID when the question takes one. It sends what was picked; the server turns that into the line the
   * CLI would read, and asks again with an error if it cannot.
   */
  let { q, sending, send }: { q: Question; sending: boolean; send: (value: Picked) => void } = $props()

  const NONE: Options = { items: [], several: false, free_text: null, mark: null }
  const options = $derived(q.options ?? NONE)
  const choices = $derived(options.items.filter((o) => !o.action && !o.alone))
  const shortcuts = $derived(options.items.filter((o) => o.alone))
  const actions = $derived(options.items.filter((o) => o.action))
  // The headings in the order they first appear, each with its options.
  const groups = $derived.by(() => {
    const found: { name: string | null; items: Option[] }[] = []
    for (const o of choices) {
      const group = found.find((g) => g.name === o.group)
      if (group) group.items.push(o)
      else found.push({ name: o.group, items: [o] })
    }
    return found
  })
  // A single question's default: marked, and focused so that Enter takes it.
  const preKey = $derived(options.several ? null : (q.preselected?.picks[0] ?? null))

  let picks = $state<string[]>([])
  let alone = $state<string | null>(null)
  let rows = $state<string[]>([''])
  let text = $state('')
  let markPick = $state<string | null>(null)
  let markRow = $state<number | null>(null)
  let armed = $state<string | null>(null)
  let defaultEl = $state<HTMLElement>()
  let lastQuestionId = ''

  // Each question starts from what its default preselects.
  $effect(() => {
    if (q.id === lastQuestionId) return
    lastQuestionId = q.id
    const pre = q.preselected ?? { picks: [], texts: [], mark: null }
    const aloneKeys = new Set(options.items.filter((o) => o.alone).map((o) => o.key))
    const ticked = pre.picks.filter((k) => !aloneKeys.has(k))
    const typed = pre.texts.length ? [...pre.texts] : ['']
    picks = ticked
    alone = pre.picks.find((k) => aloneKeys.has(k)) ?? null
    rows = typed
    text = options.several ? '' : (pre.texts[0] ?? '')
    markPick = pre.mark !== null && ticked.includes(pre.mark) ? pre.mark : null
    const at = pre.mark !== null && markPick === null ? typed.indexOf(pre.mark) : -1
    markRow = at >= 0 ? at : null
    armed = null
    void tick().then(() => {
      const active = document.activeElement
      if (defaultEl && (active === null || active === document.body)) defaultEl.focus()
    })
  })

  /** Only an http(s) link is drawn: the server drops the others, and this checks again. */
  function webLink(link: string | null): string | null {
    return link && /^https?:\/\/\S+$/i.test(link) ? link : null
  }

  function pick(o: Option) {
    send({ picks: [o.key], texts: [], mark: null })
  }

  function act(o: Option) {
    if (o.danger && armed !== o.key) {
      armed = o.key
      return
    }
    armed = null
    pick(o)
  }

  function tickOption(key: string, on: boolean) {
    alone = null
    picks = on ? [...picks, key] : picks.filter((k) => k !== key)
    if (!on && markPick === key) markPick = null
  }

  function chooseAlone(key: string) {
    alone = alone === key ? null : key
    if (alone !== null) {
      picks = []
      rows = ['']
      markPick = null
      markRow = null
    }
  }

  const texts = $derived(rows.map((r) => r.trim()).filter((r) => r !== ''))
  const nothing = $derived(alone === null && picks.length === 0 && texts.length === 0)

  function sendSeveral() {
    if (alone !== null) {
      send({ picks: [alone], texts: [], mark: null })
      return
    }
    let mark: string | null = null
    if (options.mark !== null) {
      if (markPick !== null && picks.includes(markPick)) mark = markPick
      else if (markRow !== null && (rows[markRow] ?? '').trim() !== '') mark = rows[markRow].trim()
    }
    // In the options' order, as the server puts them in the line anyway.
    const ordered = options.items.map((o) => o.key).filter((k) => picks.includes(k))
    send({ picks: ordered, texts, mark })
  }
</script>

{#snippet cardBody(o: Option, isDefault: boolean)}
  <span class="label">{o.label}{isDefault ? ' (default)' : ''}</span>
  {#if o.card && o.card.fields.length}
    <dl>
      {#each o.card.fields as [name, value], i (i)}
        <dt>{name}</dt>
        <dd>{value}</dd>
      {/each}
    </dl>
  {/if}
  {#if o.card?.note}<span class="note">{o.card.note}</span>{/if}
{/snippet}

{#snippet cardLink(o: Option)}
  {@const link = webLink(o.card?.link ?? null)}
  {#if link}
    <a class="link" href={link} target="_blank" rel="noopener noreferrer">Open the page ↗</a>
  {/if}
{/snippet}

{#snippet markRadio(checked: boolean, onpick: () => void)}
  {#if options.mark}
    <label class="mark">
      <input type="radio" name={`mark-${q.id}`} {checked} onchange={onpick} />
      {options.mark.label}
    </label>
  {/if}
{/snippet}

<div class="typed">
  {#each groups as group, g (g)}
    {@const carded = group.items.filter((o) => o.card)}
    {@const plain = group.items.filter((o) => !o.card)}
    <!-- Checkboxes go on cards only when every option has one; otherwise one list, a note under its option. -->
    {@const allCards = plain.length === 0}
    {#if group.name}<h3 class="group">{group.name}</h3>{/if}

    {#if !options.several}
      {#if carded.length}
        <div class="cards">
          {#each carded as o (o.key)}
            <div class="option-card" class:default={o.key === preKey}>
              {#if o.key === preKey}
                <button class="pick" disabled={sending} bind:this={defaultEl} onclick={() => pick(o)}>
                  {@render cardBody(o, true)}
                </button>
              {:else}
                <button class="pick" disabled={sending} onclick={() => pick(o)}>{@render cardBody(o, false)}</button>
              {/if}
              {@render cardLink(o)}
            </div>
          {/each}
        </div>
      {/if}
      {#if plain.length}
        <div class="row wrap">
          {#each plain as o (o.key)}
            {#if o.key === preKey}
              <button class="btn" disabled={sending} bind:this={defaultEl} onclick={() => pick(o)}>
                {o.label} (default)
              </button>
            {:else}
              <button class="btn secondary" disabled={sending} onclick={() => pick(o)}>{o.label}</button>
            {/if}
          {/each}
        </div>
      {/if}
    {:else}
      <div class={allCards ? 'cards' : 'checks'}>
        {#each group.items as o (o.key)}
          {@const on = picks.includes(o.key)}
          <div class={allCards ? 'option-card' : 'option-check'} class:ticked={on}>
            <label class="tick">
              <input type="checkbox" checked={on} onchange={(e) => tickOption(o.key, e.currentTarget.checked)} />
              <span class="body">{@render cardBody(o, false)}</span>
            </label>
            {#if on}
              {@render markRadio(markPick === o.key, () => {
                markPick = o.key
                markRow = null
              })}
            {/if}
            {@render cardLink(o)}
          </div>
        {/each}
      </div>
    {/if}
  {/each}

  {#if options.several && shortcuts.length}
    <div class="segmented" role="group" aria-label="Shortcuts">
      {#each shortcuts as o (o.key)}
        <button
          type="button"
          class="seg"
          class:on={alone === o.key}
          aria-pressed={alone === o.key}
          disabled={sending}
          onclick={() => chooseAlone(o.key)}>{o.label}</button
        >
      {/each}
    </div>
  {/if}

  {#if options.free_text}
    {#if options.several}
      <div class="rows">
        {#each rows as _row, i (i)}
          <div class="row">
            <input
              type={q.hide_input ? 'password' : 'text'}
              autocomplete={q.hide_input ? 'new-password' : undefined}
              class="grow mono"
              placeholder={options.free_text.placeholder}
              bind:value={rows[i]}
              oninput={() => (alone = null)}
            />
            {#if rows[i].trim() !== ''}
              {@render markRadio(markRow === i, () => {
                markRow = i
                markPick = null
              })}
            {/if}
          </div>
        {/each}
        <button type="button" class="btn small secondary" onclick={() => (rows = [...rows, ''])}>Another URL</button>
      </div>
    {:else}
      <form
        class="row free"
        onsubmit={(e) => {
          e.preventDefault()
          if (text.trim() !== '') send({ picks: [], texts: [text.trim()], mark: null })
        }}
      >
        <input
          type={q.hide_input ? 'password' : 'text'}
          autocomplete={q.hide_input ? 'new-password' : undefined}
          class="grow mono"
          bind:value={text}
          placeholder={options.free_text.placeholder}
        />
        <button class="btn" disabled={sending || text.trim() === ''}>Send</button>
      </form>
    {/if}
  {/if}

  {#if options.several}
    <div class="row send">
      <button class="btn" disabled={sending || nothing} onclick={sendSeveral}>Send</button>
    </div>
  {/if}

  {#if actions.length}
    <div class="row wrap actions">
      {#each actions as o (o.key)}
        {#if o.key === preKey}
          <button class="btn secondary" class:danger={o.danger} disabled={sending} bind:this={defaultEl} onclick={() => act(o)}>
            {armed === o.key ? `Click again to ${o.label.toLowerCase()}` : o.label} (default)
          </button>
        {:else}
          <button class="btn secondary" class:danger={o.danger} disabled={sending} onclick={() => act(o)}>
            {armed === o.key ? `Click again to ${o.label.toLowerCase()}` : o.label}
          </button>
        {/if}
      {/each}
    </div>
  {/if}
</div>

<style>
  .typed {
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
  }
  .group {
    font-size: 0.85rem;
    color: var(--text-dim);
    margin: 0.3rem 0 0;
  }
  .wrap {
    flex-wrap: wrap;
  }
  .cards {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(min(260px, 100%), 1fr));
    gap: 0.6rem;
  }
  .checks {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
  }
  .option-card {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.6rem 0.7rem;
  }
  .option-card.default,
  .option-card.ticked {
    border-color: var(--accent-dim);
  }
  .option-check {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.2rem 0.8rem;
  }
  .pick {
    all: unset;
    cursor: pointer;
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
  }
  .pick:disabled {
    cursor: default;
    opacity: 0.6;
  }
  .pick:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 4px;
    border-radius: 4px;
  }
  .option-card:has(.pick:hover:not(:disabled)) {
    background: var(--bg-hover);
  }
  .tick {
    display: flex;
    gap: 0.5rem;
    align-items: flex-start;
    cursor: pointer;
  }
  .tick input {
    margin-top: 0.3rem;
  }
  .body {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
  }
  .label {
    font-weight: 600;
    overflow-wrap: anywhere;
  }
  dl {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 0.1rem 0.6rem;
    margin: 0;
    font-size: 0.82rem;
  }
  dt {
    color: var(--text-dim);
  }
  dd {
    margin: 0;
    overflow-wrap: anywhere;
  }
  .note {
    color: var(--warn);
    font-size: 0.82rem;
  }
  .link {
    font-size: 0.82rem;
    align-self: flex-start;
  }
  .mark {
    font-size: 0.82rem;
    color: var(--text-dim);
    display: inline-flex;
    gap: 0.3rem;
    align-items: center;
    cursor: pointer;
    white-space: nowrap;
  }
  .segmented {
    display: inline-flex;
    align-self: flex-start;
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
  }
  .seg {
    background: var(--bg-hover);
    color: var(--text);
    border: none;
    border-right: 1px solid var(--border);
    padding: 0.35rem 0.8rem;
    font-size: 0.85rem;
    cursor: pointer;
  }
  .seg:last-child {
    border-right: none;
  }
  .seg.on {
    background: var(--accent);
    color: #1a1214;
    font-weight: 600;
  }
  .seg:disabled {
    opacity: 0.45;
    cursor: default;
  }
  .rows {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    align-items: flex-start;
  }
  .rows .row {
    width: 100%;
  }
  .actions {
    padding-top: 0.5rem;
    border-top: 1px solid var(--border);
  }
  .danger {
    color: var(--err);
    border-color: var(--err);
  }
  @media (max-width: 700px) {
    input[type='text'] {
      font-size: 16px;
    }
  }
</style>
