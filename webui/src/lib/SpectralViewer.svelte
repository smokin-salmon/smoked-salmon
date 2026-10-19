<script lang="ts">
  /**
   * One spectral at a time with previous / next. The root takes the keys itself (never a window listener), so
   * several viewers on one page each answer only their own: ArrowLeft / ArrowRight step, and Enter calls `onenter`
   * when the root itself has the focus (Enter on a focused button keeps activating that button).
   */
  let {
    images,
    index = $bindable(0),
    onenter,
    root = $bindable(),
  }: {
    images: { url: string; label: string }[]
    index: number
    onenter?: () => void
    root?: HTMLElement
  } = $props()

  const current = $derived(images[index])

  // Preload the neighbours so stepping is instant.
  $effect(() => {
    for (const i of [index - 1, index + 1]) {
      const image = images[i]
      if (image) new Image().src = image.url
    }
  })

  function step(by: number) {
    const next = index + by
    if (next >= 0 && next < images.length) index = next
  }

  function onkeydown(event: KeyboardEvent) {
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return
    if (event.key === 'ArrowLeft') {
      step(-1)
      event.preventDefault()
    } else if (event.key === 'ArrowRight') {
      step(1)
      event.preventDefault()
    } else if (event.key === 'Enter' && onenter && event.target === event.currentTarget) {
      onenter()
      event.preventDefault()
    }
  }
</script>

<!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
<div class="viewer" role="group" aria-label="Spectral" tabindex="0" bind:this={root} {onkeydown} onclick={() => root?.focus()}>
  {#if current}
    <a href={current.url} target="_blank" rel="noreferrer" class="image">
      <img src={current.url} alt={current.label} />
    </a>
    <div class="bar">
      <button class="btn small secondary" disabled={index <= 0} onclick={() => step(-1)}>Previous</button>
      <span class="label muted mono">{current.label}</span>
      <span class="pos muted">{index + 1} / {images.length}</span>
      <button class="btn small secondary" disabled={index >= images.length - 1} onclick={() => step(1)}>Next</button>
    </div>
  {/if}
</div>

<style>
  .viewer {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.5rem;
    max-width: 100%;
  }
  .viewer:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 3px;
  }
  .image {
    display: block;
    max-width: 100%;
  }
  img {
    display: block;
    max-width: 100%;
    max-height: calc(100vh - 240px);
    object-fit: contain;
    border: 1px solid var(--border);
    border-radius: 6px;
  }
  .bar {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.7rem;
    flex-wrap: wrap;
    max-width: 100%;
  }
  .label {
    font-size: 0.8rem;
    overflow-wrap: anywhere;
    text-align: center;
  }
  .pos {
    font-size: 0.85rem;
    white-space: nowrap;
  }
</style>
