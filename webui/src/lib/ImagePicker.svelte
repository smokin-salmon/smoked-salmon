<script lang="ts">
  import { apiGet } from './api'

  let { selected = $bindable([]), max }: { selected: string[]; max: number } = $props()

  interface Folder {
    name: string
    path: string
    library?: boolean
  }

  interface Picture {
    name: string
    path: string
    size: number
  }

  interface Listing {
    path: string | null
    parent: string | null
    folders: Folder[]
    images: Picture[]
    truncated: boolean
    roots: { path: string; name: string; library: boolean }[]
  }

  let listing = $state<Listing | null>(null)
  let error = $state('')
  let loading = $state(false)

  /** The roots (no path), or the folders and image files in one; the server refuses anything outside the roots. */
  async function browse(path?: string | null) {
    loading = true
    error = ''
    try {
      const query = path ? `path=${encodeURIComponent(path)}&images=1` : 'images=1'
      listing = await apiGet<Listing>(`/browse?${query}`)
    } catch (e) {
      error = String(e)
    } finally {
      loading = false
    }
  }

  function within(root: string, path: string): boolean {
    return path === root || path.startsWith(root.endsWith('/') ? root : root + '/')
  }

  function toggle(picture: Picture) {
    if (selected.includes(picture.path)) selected = selected.filter((path) => path !== picture.path)
    else if (selected.length < max) selected = [...selected, picture.path]
  }

  function size(bytes: number): string {
    if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MiB`
    return `${Math.max(1, Math.round(bytes / 1024))} KiB`
  }

  function name(path: string): string {
    return path.slice(path.lastIndexOf('/') + 1)
  }
</script>

<!-- The picker is the way in: the server reads each file again, and sends it only if it is an image by what is in
     it (JPEG, PNG, GIF or WebP), whatever its name says. Ported from the fork's Tools page (chodeus 0b29d2d5). -->
<div class="picker">
  <div class="row">
    <button class="btn secondary" disabled={loading} onclick={() => browse(listing?.path ?? null)}>
      Browse for images
    </button>
    <span class="muted">{selected.length} of {max} picked</span>
  </div>

  {#if selected.length}
    <ul class="picked">
      {#each selected as path (path)}
        <li>
          <span class="mono grow" title={path}>{name(path)}</span>
          <button class="btn small secondary" onclick={() => (selected = selected.filter((p) => p !== path))}>
            Remove
          </button>
        </li>
      {/each}
    </ul>
  {/if}

  {#if listing}
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
        <button class="btn small secondary" onclick={() => (listing = null)}>Close</button>
      </div>
      <ul>
        {#if listing.parent}
          <li><button class="nav" onclick={() => browse(listing!.parent)}>..</button></li>
        {:else if listing.path}
          <li><button class="nav" onclick={() => browse(null)}>..</button></li>
        {/if}
        {#each listing.folders as folder (folder.path)}
          <li><button class="nav" onclick={() => browse(folder.path)}>{folder.name}/</button></li>
        {/each}
        {#each listing.images as picture (picture.path)}
          <li>
            <label class="pick">
              <input
                type="checkbox"
                checked={selected.includes(picture.path)}
                disabled={!selected.includes(picture.path) && selected.length >= max}
                onchange={() => toggle(picture)}
              />
              <span class="mono">{picture.name}</span>
              <span class="muted">{size(picture.size)}</span>
            </label>
          </li>
        {/each}
        {#if !listing.folders.length && !listing.images.length}
          <li class="muted">No folders or images here.</li>
        {/if}
      </ul>
      {#if listing.truncated}
        <p class="muted small">Only the first entries are listed: this folder holds more.</p>
      {/if}
    </div>
  {/if}
  {#if error}<p class="error">{error}</p>{/if}
</div>

<style>
  .picked {
    list-style: none;
    padding: 0;
    margin: 0.6rem 0 0;
  }
  .picked li {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.15rem 0;
  }
  .picked .mono {
    overflow-wrap: anywhere;
  }
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
  .here {
    flex-wrap: wrap;
    margin-bottom: 0.3rem;
  }
  .here .mono {
    overflow-wrap: anywhere;
  }
  ul {
    list-style: none;
    padding: 0;
    margin: 0;
  }
  .listing li {
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
  .pick {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.15rem 0.3rem;
    cursor: pointer;
    overflow-wrap: anywhere;
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
