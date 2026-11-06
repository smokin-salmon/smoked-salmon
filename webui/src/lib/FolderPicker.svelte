<script lang="ts">
  import { tick } from 'svelte'
  import { ApiError, apiGet } from './api'

  /**
   * `value`: the folder the job uses, set by a Select or by typing it (a page empties it after a start); it starts
   *   empty.
   * `empty`: what the job uses when nothing is chosen, for a page where the folder is optional.
   * `usable`: set by the picker, true once the server has checked the folder in the box and not refused it (or could
   *   not check it: the job checks it again). A page starts a job on the folder only then.
   */
  let {
    value = $bindable(''),
    empty = 'nothing chosen',
    usable = $bindable(false),
  }: { value: string; empty?: string; usable?: boolean } = $props()

  interface Folder {
    name: string
    path: string
    audio: boolean
    library?: boolean
  }

  interface Listing {
    path: string | null
    missing: string | null
    parent: string | null
    library: boolean
    audio: boolean
    refused: string | null
    folders: Folder[]
    truncated: boolean
    sort: Sort
    roots: { path: string; name: string; library: boolean }[]
  }

  interface Summary {
    path: string
    summary: string
  }

  type Sort = 'newest' | 'name'
  const SORT_KEY = 'salmon.folderSort'
  const LAST_KEY = 'salmon.folderLast'
  const DEBOUNCE_MS = 400

  /** The viewer's last choice; blocked or cleared storage (a private window) falls back to newest first. */
  function remembered(): Sort {
    try {
      return localStorage.getItem(SORT_KEY) === 'name' ? 'name' : 'newest'
    } catch {
      return 'newest'
    }
  }

  /** The last folder browsed in this browser, where Browse opens; none (or blocked storage) opens the roots. */
  function lastBrowsed(): string | null {
    try {
      return localStorage.getItem(LAST_KEY)
    } catch {
      return null
    }
  }

  /** Remembered for Browse only: it never becomes the job's folder. */
  function rememberBrowsed(path: string | null) {
    try {
      if (path) localStorage.setItem(LAST_KEY, path)
      else localStorage.removeItem(LAST_KEY)
    } catch {
      // Not remembered: the next Browse opens the roots.
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
   * What the server says about the folder in the box: the folder it resolves it to and one line counting its audio
   * files, its reason for refusing it (a 4xx answer), or that the folder could not be checked (no answer, or a 5xx).
   */
  let checked = $state<{ value: string; path: string; line: string; kind: 'count' | 'refused' | 'failed' } | null>(
    null,
  )
  const current = $derived(checked?.value === value ? checked : null)
  // The folder the job would use: the one the server resolved the box to, once it has.
  const shownPath = $derived(current?.path || value)

  $effect(() => {
    usable = !!value && current !== null && current.kind !== 'refused'
  })

  $effect(() => {
    const folder = value
    if (!folder) {
      checked = null
      return
    }
    let stale = false
    const timer = setTimeout(() => {
      apiGet<Summary>(`/folder?path=${encodeURIComponent(folder)}`)
        .then((found) => {
          if (!stale) checked = { value: folder, path: found.path, line: found.summary, kind: 'count' }
        })
        .catch((e) => {
          if (stale) return
          const line = e instanceof Error ? e.message : String(e)
          if (e instanceof ApiError && e.status === 401) {
            // Logged out: the login screen takes over.
            checked = null
          } else if (e instanceof ApiError && e.status >= 400 && e.status < 500) {
            checked = { value: folder, path: '', line, kind: 'refused' }
          } else {
            checked = { value: folder, path: '', line: `Could not check this folder: ${line}`, kind: 'failed' }
          }
        })
    }, DEBOUNCE_MS)
    return () => {
      stale = true
      clearTimeout(timer)
    }
  })

  let latest = 0

  /**
   * The roots (no path), or the folders in one; the server refuses anything outside the roots, and lists the nearest
   * folder above one that is gone. Only the latest request applies its answer, so a slower, older one (another
   * folder, the other sort) cannot overwrite it. The folder listed is remembered for Browse unless `remember` is
   * false. The error when the latest request failed, else null.
   */
  async function browse(path?: string | null, focus = false, remember = true): Promise<unknown> {
    const mine = ++latest
    loading = true
    error = ''
    try {
      const query = path ? `path=${encodeURIComponent(path)}&sort=${sort}` : `sort=${sort}`
      const found = await apiGet<Listing>(`/browse?${query}`)
      if (mine !== latest) return null
      if ((found.path ?? null) !== (listing?.path ?? null)) filter = ''
      listing = found
      open = true
      if (remember) rememberBrowsed(found.path)
      if (focus && window.matchMedia('(pointer: fine)').matches) {
        await tick()
        if (mine === latest) filterInput?.focus()
      }
    } catch (e) {
      if (mine !== latest) return null
      error = String(e)
      return e
    } finally {
      if (mine === latest) loading = false
    }
    return null
  }

  /**
   * Browse opens at the folder in the box, else at the last folder browsed, else at the roots; the server lists the
   * nearest folder above one that is gone. A remembered folder the server refuses (400, 403, 404: the roots changed)
   * is forgotten; one it could not answer for is kept.
   */
  async function openBrowser() {
    const last = lastBrowsed()
    for (const start of new Set([value, last])) {
      if (!start) continue
      const failed = await browse(start, true)
      if (failed === null) return
      if (start === last && failed instanceof ApiError && [400, 403, 404].includes(failed.status)) {
        rememberBrowsed(null)
      }
    }
    await browse(null, true, false)
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
     select button on each row, apart from the name that opens it. Only folders with audio files in them or in their
     disc folders can be picked. -->
<div class="picker">
  <div class="row">
    <input
      type="text"
      class="mono grow"
      bind:value
      placeholder="Browse, or type an album folder's path"
      aria-label="Album folder"
    />
    <button class="btn secondary" disabled={loading} onclick={openBrowser}>Browse</button>
  </div>
  <p class="small will-use">
    {#if current?.kind === 'refused'}
      Cannot use: <span class="mono">{value}</span>
    {:else if value && !current}
      Checking: <span class="mono">{value}</span>
    {:else}
      Will use: {#if value}<span class="mono">{shownPath}</span>{:else}<span class="muted">{empty}</span>{/if}
    {/if}
  </p>
  <p
    class="small checked"
    class:error={current?.kind === 'refused'}
    class:muted={current?.kind !== 'refused'}
    aria-live="polite"
  >
    {#if current}{current.line}{/if}
  </p>

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
      {#if listing.missing}
        <p class="muted small missing">
          There is no folder <span class="mono">{listing.missing}</span>: this is the nearest folder above it.
        </p>
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
      {#if listing.refused}
        <p class="small error refused">Cannot select this folder: {listing.refused}</p>
      {/if}
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
              <span class="chip ok" title="Audio files in it or its disc folders">audio</span>
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
  /* Kept in the page while empty, so a screen reader hears what arrives in it; it takes no room then. */
  .checked:empty {
    margin: 0;
  }
  .will-use .mono,
  .missing .mono {
    overflow-wrap: anywhere;
  }
  .missing,
  .refused {
    margin: 0 0 0.4rem;
  }
  .error {
    color: var(--err);
    margin: 0.4rem 0 0;
    font-size: 0.85rem;
  }
</style>
