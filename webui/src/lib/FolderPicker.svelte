<script lang="ts">
  import { onMount, tick } from 'svelte'
  import { apiGet } from './api'

  /** `prefill`: start an empty box with download_directory and a separator, for a page that needs an album folder. */
  let { value = $bindable(''), prefill = true }: { value: string; prefill?: boolean } = $props()

  interface Folder {
    name: string
    path: string
    audio: boolean
    library?: boolean
  }

  interface Listing {
    path: string | null
    parent: string | null
    library: boolean
    audio: boolean
    folders: Folder[]
    truncated: boolean
    sort: Sort
    roots: { path: string; name: string; library: boolean }[]
  }

  type Sort = 'newest' | 'name'
  const SORT_KEY = 'salmon.folderSort'

  /** The viewer's last choice; blocked or cleared storage (a private window) falls back to newest first. */
  function remembered(): Sort {
    try {
      return localStorage.getItem(SORT_KEY) === 'name' ? 'name' : 'newest'
    } catch {
      return 'newest'
    }
  }

  let open = $state(false)
  let listing = $state<Listing | null>(null)
  let error = $state('')
  let loading = $state(false)
  let sort = $state<Sort>(remembered())
  let filter = $state('')
  let filterInput = $state<HTMLInputElement | null>(null)

  /** Case- and accent-insensitive, so `cafe` finds `Café`. */
  function fold(s: string): string {
    return s.normalize('NFD').replace(/\p{M}/gu, '').toLowerCase()
  }

  const words = $derived(fold(filter).split(/\s+/).filter(Boolean))
  const filtering = $derived(words.length > 0)
  const shown = $derived(
    listing ? listing.folders.filter((f) => words.every((w) => fold(f.name).includes(w))) : [],
  )

  /**
   * An empty box starts as download_directory and a separator, so a folder name pasted after it is an absolute path
   * and Browse opens inside download_directory. The first root is always download_directory. A failure leaves the
   * box empty, and a value set meanwhile (the page's, or typed) is never overwritten.
   */
  onMount(async () => {
    if (!prefill || value) return
    try {
      const answer = await apiGet<Listing>('/browse')
      const root = answer.roots[0]
      if (value || !root || root.library) return
      const separator = root.path.includes('\\') ? '\\' : '/'
      value = /[\\/]$/.test(root.path) ? root.path : root.path + separator
    } catch {
      // Not prefilled: the box stays empty.
    }
  })

  let latest = 0

  /**
   * The roots (no path), or the folders in one; the server refuses anything outside the roots. Only the latest
   * request applies its answer, so a slower, older one (another folder, the other sort) cannot overwrite it.
   */
  async function browse(path?: string | null, focus = false) {
    const mine = ++latest
    loading = true
    error = ''
    try {
      const query = path ? `path=${encodeURIComponent(path)}&sort=${sort}` : `sort=${sort}`
      const found = await apiGet<Listing>(`/browse?${query}`)
      if (mine !== latest) return
      if ((found.path ?? null) !== (listing?.path ?? null)) filter = ''
      listing = found
      open = true
      if (focus && window.matchMedia('(pointer: fine)').matches) {
        await tick()
        if (mine === latest) filterInput?.focus()
      }
    } catch (e) {
      if (mine === latest) error = String(e)
    } finally {
      if (mine === latest) loading = false
    }
  }

  function sortBy(next: Sort) {
    if (next === sort) return
    sort = next
    try {
      localStorage.setItem(SORT_KEY, next)
    } catch {
      // Not remembered: the choice still holds until the page is closed.
    }
    if (listing) void browse(listing.path)
  }

  function closeListing() {
    open = false
    filter = ''
  }

  function filterKey(event: KeyboardEvent) {
    if (event.isComposing || event.keyCode === 229) return
    if (event.key === 'Escape' && filter) {
      event.preventDefault()
      filter = ''
    } else if (event.key === 'Enter') {
      event.preventDefault()
      if (shown.length === 1) void browse(shown[0].path)
    }
  }

  function select(path: string) {
    value = path
    closeListing()
  }

  function within(root: string, path: string): boolean {
    return path === root || path.startsWith(root.endsWith('/') ? root : root + '/')
  }
</script>

<!-- Ported from the fork's FolderPicker (chodeus a18e0e25, styx-techno 6e91041a): roots side by side, and a
     select button on each row, apart from the name that opens it. Only folders holding audio can be picked. -->
<div class="picker">
  <div class="row">
    <input type="text" class="mono grow" bind:value placeholder="/path/to/album" />
    <button class="btn secondary" disabled={loading} onclick={() => browse(value || null, true)}>Browse</button>
  </div>

  {#if open && listing}
    <div class="listing">
      {#if listing.roots.length > 1}
        <div class="roots">
          {#each listing.roots as root (root.path)}
            <button
              class="root"
              class:current={listing.path !== null && within(root.path, listing.path)}
              onclick={() => browse(root.path)}
            >
              {root.name}{root.library ? ' (library)' : ''}
            </button>
          {/each}
        </div>
      {/if}
      <div class="row here">
        <span class="mono grow">{listing.path ?? 'Your folders'}</span>
        {#if listing.path && listing.audio}
          <button class="btn small" onclick={() => select(listing!.path!)}>Select this folder</button>
        {/if}
        {#if listing.path}
          <span
            class="sorts"
            role="group"
            aria-label="Sort folders"
            title="Newest is by when the folder was last changed or copied in"
          >
            <button class="sort" class:current={sort === 'newest'} aria-pressed={sort === 'newest'} onclick={() => sortBy('newest')}
              >Newest</button
            >
            <button class="sort" class:current={sort === 'name'} aria-pressed={sort === 'name'} onclick={() => sortBy('name')}
              >Name</button
            >
          </span>
        {/if}
        <button class="btn small secondary" onclick={closeListing}>Close</button>
      </div>
      {#if listing.folders.length > 0}
        <input
          type="text"
          class="filter"
          bind:this={filterInput}
          bind:value={filter}
          onkeydown={filterKey}
          placeholder="Filter these folders"
          aria-label="Filter folders"
        />
        {#if filtering}
          <p class="muted small count">{shown.length} of {listing.folders.length} folders</p>
        {/if}
      {/if}
      <ul>
        {#if listing.parent}
          <li><button class="nav" onclick={() => browse(listing!.parent)}>..</button></li>
        {:else if listing.path}
          <li><button class="nav" onclick={() => browse(null)}>..</button></li>
        {/if}
        {#each shown as folder (folder.path)}
          <li>
            <button class="nav" onclick={() => browse(folder.path)}>
              {folder.name}/{#if folder.library}<span class="muted"> (library)</span>{/if}
            </button>
            {#if folder.audio}
              <span class="chip ok" title="Holds audio files">audio</span>
              <button class="btn small secondary" onclick={() => select(folder.path)}>Select</button>
            {/if}
          </li>
        {:else}
          <li class="muted">
            {#if filtering}No folder here matches "{filter}".{:else}No folders here.{/if}
          </li>
        {/each}
      </ul>
      {#if listing.truncated}
        <p class="muted small">
          {#if filtering}The filter only covers the first {listing.folders.length} folders listed here{:else}Only the first {listing.folders.length}
            folders are listed{/if}: type the path to reach another.
        </p>
      {/if}
    </div>
  {/if}
  {#if error}<p class="error">{error}</p>{/if}
</div>

<style>
  .listing {
    margin-top: 0.5rem;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.6rem 0.7rem;
    max-height: 340px;
    overflow-y: auto;
    background: var(--bg);
  }
  .roots {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
    margin-bottom: 0.5rem;
  }
  .root {
    font-size: 0.78rem;
    padding: 0.25rem 0.55rem;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--bg-raised);
    color: var(--text-dim);
    cursor: pointer;
  }
  .root.current {
    border-color: var(--accent);
    color: var(--text);
  }
  .sorts {
    display: inline-flex;
    gap: 0.25rem;
  }
  .sort {
    font-size: 0.78rem;
    padding: 0.2rem 0.5rem;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--bg-raised);
    color: var(--text-dim);
    cursor: pointer;
  }
  .sort.current {
    border-color: var(--accent);
    color: var(--text);
  }
  .here {
    flex-wrap: wrap;
    margin-bottom: 0.3rem;
  }
  .here .mono {
    overflow-wrap: anywhere;
  }
  .filter {
    width: 100%;
    box-sizing: border-box;
    margin-bottom: 0.3rem;
  }
  .count {
    margin: 0 0 0.3rem;
  }
  ul {
    list-style: none;
    padding: 0;
    margin: 0;
  }
  li {
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }
  li button.nav {
    background: none;
    border: none;
    color: var(--text);
    cursor: pointer;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.85rem;
    padding: 0.15rem 0.3rem;
    flex: 1;
    text-align: left;
    border-radius: 5px;
    overflow-wrap: anywhere;
  }
  li button.nav:hover {
    background: var(--bg-hover);
    color: var(--accent);
  }
  .small {
    font-size: 0.8rem;
    margin: 0.4rem 0 0;
  }
  .error {
    color: var(--err);
    margin: 0.4rem 0 0;
    font-size: 0.85rem;
  }
</style>
